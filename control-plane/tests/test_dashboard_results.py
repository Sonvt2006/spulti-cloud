"""Final results remain owner-only and downloadable after machine deletion."""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_dashboard_jobs as fixtures
from app.models import AgentJob, Machine


class DashboardResultTests(unittest.TestCase):
    setUp = fixtures.DashboardJobTests.setUp
    tearDown = fixtures.DashboardJobTests.tearDown
    headers = fixtures.DashboardJobTests.headers
    submit_job = fixtures.DashboardJobTests.submit_job

    def completed(self, *, state=None, version=3):
        created = self.submit_job().json()
        job = self.db.get(AgentJob, created["job_id"])
        checkpoint = {
            "version": version,
            "job_id": job.id,
            "total_steps": job.total_steps,
            "last_completed_step": job.total_steps,
            "status": "COMPLETED",
        }
        if version == 3:
            checkpoint.update(
                workload="python_script",
                workload_state=state if state is not None else {"answer": 42},
                source_sha256="a" * 64,
            )
        job.status = "COMPLETED"
        job.current_step = job.checkpoint_step = job.total_steps
        job.checkpoint_data = checkpoint
        self.db.get(Machine, created["machine"]["id"]).status = "DELETED"
        self.db.commit()
        return job, checkpoint

    def test_inline_result_and_attachment_after_instance_deleted(self):
        state = {"answer": 9007199254740993, "text": "Kết quả <script>test</script>"}
        job, checkpoint = self.completed(state=state)
        url = f"/api/dashboard/jobs/{job.id}/result"
        response = self.client.get(url, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(json.loads(result["result_json"]), state)
        self.assertEqual(json.loads(result["checkpoint_json"]), checkpoint)
        self.assertIn("9007199254740993", result["result_json"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        download = self.client.get(url + "?download=true", headers=self.headers())
        self.assertEqual(download.status_code, 200, download.text)
        self.assertEqual(download.json(), checkpoint)
        self.assertEqual(download.text, result["checkpoint_json"])
        self.assertEqual(download.headers["content-type"], "application/json")
        self.assertEqual(
            download.headers["content-disposition"],
            f'attachment; filename="job-{job.id}-final.json"',
        )
        self.assertNotIn("agent_token", download.text)

    def test_s3_checkpoint_download_retains_file_snapshot_reference(self):
        job, checkpoint = self.completed()
        checkpoint["file_snapshot"] = {
            "key": f"checkpoints/{job.id}/files/example.tar",
            "size": 1024,
            "sha256": "b" * 64,
        }
        job.checkpoint_data = {"storage": "s3", "key": "final.json"}
        self.db.commit()
        with patch("app.dashboard.S3CheckpointStore") as storage:
            storage.return_value.load.return_value = checkpoint
            response = self.client.get(
                f"/api/dashboard/jobs/{job.id}/result?download=true",
                headers=self.headers(),
            )
            storage.return_value.load.assert_called_once_with(job.checkpoint_data)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), checkpoint)

    def test_auth_and_ownership_checked_before_accessing_storage(self):
        job, _ = self.completed()
        with patch("app.dashboard.S3CheckpointStore") as storage:
            for suffix in ("", "?download=true"):
                url = f"/api/dashboard/jobs/{job.id}/result{suffix}"
                self.assertEqual(self.client.get(url).status_code, 401)
                self.assertEqual(
                    self.client.get(url, headers=self.headers(self.other)).status_code,
                    404,
                )
            self.assertEqual(
                self.client.get(
                    f"/api/dashboard/jobs/{uuid4()}/result", headers=self.headers()
                ).status_code,
                404,
            )
            storage.assert_not_called()

    def test_unfinished_job_cannot_export_partial_checkpoint_as_final(self):
        job, _ = self.completed()
        with patch("app.dashboard.S3CheckpointStore") as storage:
            for status, step in (("RUNNING", 1), ("INTERRUPTED", 1), ("COMPLETED", 0)):
                job.status, job.checkpoint_step = status, step
                self.db.commit()
                response = self.client.get(
                    f"/api/dashboard/jobs/{job.id}/result?download=true",
                    headers=self.headers(),
                )
                self.assertEqual(response.status_code, 409)
            storage.assert_not_called()

    def test_missing_and_unavailable_storage_can_be_retried(self):
        job, checkpoint = self.completed()
        url = f"/api/dashboard/jobs/{job.id}/result"
        job.checkpoint_data = None
        self.db.commit()
        self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 404)
        job.checkpoint_data = {"storage": "s3", "key": "final.json"}
        self.db.commit()
        with patch("app.dashboard.S3CheckpointStore") as storage:
            storage.return_value.load.side_effect = RuntimeError("private bucket")
            response = self.client.get(url, headers=self.headers())
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private bucket", response.text)
            storage.return_value.load.side_effect = None
            storage.return_value.load.return_value = checkpoint
            self.assertEqual(
                self.client.get(url, headers=self.headers()).status_code, 200
            )

    def test_wrong_or_invalid_checkpoint_is_not_exported(self):
        job, checkpoint = self.completed()
        job.checkpoint_data = {"storage": "s3", "key": "final.json"}
        self.db.commit()
        with patch("app.dashboard.S3CheckpointStore") as storage:
            for changes in (
                {"job_id": str(uuid4())},
                {"status": "RUNNING"},
                {"last_completed_step": 0},
                {"total_steps": 2},
                {"version": 99},
            ):
                with self.subTest(changes=changes):
                    storage.return_value.load.return_value = {**checkpoint, **changes}
                    response = self.client.get(
                        f"/api/dashboard/jobs/{job.id}/result?download=true",
                        headers=self.headers(),
                    )
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn("content-disposition", response.headers)

    def test_legacy_checkpoint_without_state_still_downloads(self):
        job, checkpoint = self.completed(version=1)
        response = self.client.get(
            f"/api/dashboard/jobs/{job.id}/result", headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["result_json"])
        self.assertEqual(json.loads(response.json()["checkpoint_json"]), checkpoint)


if __name__ == "__main__":
    unittest.main()
