"""Recovery across cloud failures, agent fencing, cooldown and deletion races."""

import hashlib
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import test_instance_manager as fixtures
from app.models import (
    AgentJob,
    Instance,
    InstanceAttempt,
    MachineJob,
    MachineLifecycle,
    NodeAgent,
    RecoveryRun,
)
from sqlalchemy import func, select


class RecoveryTests(unittest.TestCase):
    tearDown = fixtures.InstanceManagerTests.tearDown
    headers = fixtures.InstanceManagerTests.headers
    submit = fixtures.InstanceManagerTests.submit
    create = fixtures.InstanceManagerTests.create
    tick = fixtures.InstanceManagerTests.tick
    view = fixtures.InstanceManagerTests.view
    provision = fixtures.InstanceManagerTests.provision

    def setUp(self):
        fixtures.InstanceManagerTests.setUp(self)
        env = patch.dict(
            os.environ,
            {
                "SPULTI_PUBLIC_API_URL": "https://api.example.test",
                "SPULTI_AGENT_RELEASE_URL": "https://releases.example.test/agent.tar.gz",
                "SPULTI_AGENT_RELEASE_SHA256": "a" * 64,
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def running(self, managed=False, provider=None):
        job = None
        if managed:
            response = self.client.post(
                "/api/agent/jobs", headers=self.headers(), json={}
            )
            self.assertEqual(response.status_code, 201, response.text)
            job = response.json()
        machine_id = self.provision(
            **({"provider": provider} if provider else {}),
            **({"job_id": job["job_id"]} if job else {}),
        )
        cloud = self.gcp if provider == "GCP" else self.cloud
        key = cloud.specs[0].request_id
        cloud.instances[key] = replace(cloud.instances[key], status="running")
        self.tick(machine_id)
        return machine_id, job

    def current_token(self, machine_id):
        from app.services.managed_agent import attempt_token

        self.db.expire_all()
        binding = self.db.get(MachineJob, machine_id)
        attempt = self.db.get(InstanceAttempt, binding.active_attempt_id)
        return attempt_token(attempt.request_id)

    def heartbeat(self, job_id, token, status="RUNNING"):
        return self.client.post(
            f"/api/agent/jobs/{job_id}/heartbeat",
            headers={"X-Agent-Token": token},
            json={"current_step": 0, "last_checkpoint_step": 0, "status": status},
        )

    def notice(self, job_id, token, event_id=None):
        return self.client.post(
            f"/api/agent/jobs/{job_id}/interruptions",
            headers={"X-Agent-Token": token},
            json={
                "event_id": event_id or str(uuid4()),
                "provider": "aws",
                "action": "terminate",
                "detected_at": "2026-01-01T00:00:00+00:00",
                "current_step": 0,
                "last_checkpoint_step": 0,
                "local_checkpoint_step": 0,
                "checkpoint_status": "failed",
                "checkpoint_error": "upload_failed",
            },
        )

    def test_lost_vm_replans_to_other_cloud_and_records_one_event(self):
        machine_id, _ = self.running()
        self.cloud.instances.clear()
        self.tick(machine_id)  # detect disappearance
        self.tick(machine_id)  # re-rank excluding AWS
        self.tick(machine_id)  # create GCP
        node = self.db.scalar(
            select(NodeAgent)
            .join(InstanceAttempt)
            .where(InstanceAttempt.provider == "GCP", InstanceAttempt.state == "ACTIVE")
        )
        node.last_heartbeat_at = datetime.now(UTC)
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(len(self.gcp.specs), 1)
        run = self.db.scalar(select(RecoveryRun))
        self.assertEqual(run.reason, "INSTANCE_LOST")
        self.assertEqual(run.state, "SUCCEEDED")
        self.assertIsNotNone(run.replacement_attempt_id)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(RecoveryRun)), 1
        )
        events = self.client.get(
            f"/api/machines/{machine_id}/events", headers=self.headers()
        )
        self.assertGreaterEqual(events.json()[0]["downtime_seconds"], 0)
        self.assertEqual(
            self.client.get(
                f"/api/machines/{machine_id}/events", headers=self.headers(self.other)
            ).status_code,
            404,
        )

    def test_interruption_fences_old_agent_and_waits_for_new_heartbeat(self):
        machine_id, job = self.running(managed=True)
        token = self.current_token(machine_id)
        self.assertEqual(self.heartbeat(job["job_id"], token).status_code, 200)
        event_id = str(uuid4())
        self.assertEqual(self.notice(job["job_id"], token, event_id).status_code, 200)
        self.assertEqual(self.notice(job["job_id"], token, event_id).status_code, 200)
        self.tick(machine_id)  # durable recovery + fence
        self.assertEqual(self.heartbeat(job["job_id"], token).status_code, 401)
        self.tick(machine_id)  # delete AWS
        self.assertEqual(len(self.gcp.specs), 0)
        self.tick(machine_id)  # confirm deletion
        self.tick(machine_id)  # plan
        self.tick(machine_id)  # start GCP
        self.assertEqual(self.view(machine_id)["status"], "RECOVERING")
        new_token = self.current_token(machine_id)
        self.assertNotEqual(token, new_token)
        self.assertEqual(self.notice(job["job_id"], token).status_code, 401)
        self.assertEqual(self.heartbeat(job["job_id"], new_token).status_code, 200)
        binding = self.db.get(MachineJob, machine_id)
        self.db.get(
            NodeAgent, binding.active_attempt_id
        ).last_heartbeat_at = datetime.now(UTC)
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "RUNNING")
        self.assertEqual(self.db.scalar(select(RecoveryRun)).state, "SUCCEEDED")
        self.assertIn("agent.py managed", self.gcp.specs[0].startup_script)
        self.assertNotIn(token, self.gcp.specs[0].startup_script)

    def test_missing_heartbeat_and_boot_grace(self):
        machine_id, job = self.running(managed=True)
        self.tick(machine_id)
        self.assertIsNone(self.db.scalar(select(RecoveryRun)))
        binding = self.db.get(MachineJob, machine_id)
        binding.activated_at = datetime.now(UTC) - timedelta(seconds=601)
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(self.db.scalar(select(RecoveryRun)).reason, "BOOT_TIMEOUT")

    def test_stale_heartbeat_starts_recovery(self):
        machine_id, created = self.running(managed=True)
        binding = self.db.get(MachineJob, machine_id)
        binding.activated_at = datetime.now(UTC) - timedelta(seconds=300)
        job = self.db.get(AgentJob, created["job_id"])
        job.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=91)
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(
            self.db.scalar(select(RecoveryRun)).reason, "HEARTBEAT_TIMEOUT"
        )

    def test_completed_job_does_not_trigger_heartbeat_recovery(self):
        machine_id, created = self.running(managed=True)
        binding = self.db.get(MachineJob, machine_id)
        binding.activated_at = datetime.now(UTC) - timedelta(hours=1)
        self.db.get(AgentJob, created["job_id"]).status = "COMPLETED"
        self.db.commit()
        self.tick(machine_id)
        self.assertIsNone(self.db.scalar(select(RecoveryRun)))

    def complete(self, machine_id, created, *, fail=False):
        """Exercise the actual completion API; no final heartbeat is required."""
        with patch("app.agent_api.S3CheckpointStore") as storage:
            storage.return_value.save.return_value = {
                "storage": "s3",
                "key": "final-checkpoint.json",
                "status": "COMPLETED",
            }
            if fail:
                storage.return_value.save.side_effect = RuntimeError("unavailable")
            return self.client.put(
                f"/api/agent/jobs/{created['job_id']}/checkpoint",
                headers={"X-Agent-Token": self.current_token(machine_id)},
                json={
                    "version": 1,
                    "job_id": created["job_id"],
                    "total_steps": created["total_steps"],
                    "last_completed_step": created["total_steps"],
                    "status": "COMPLETED",
                },
            )

    def test_final_checkpoint_deletes_aws_and_gcp_without_replacement(self):
        for provider in ("AWS", "GCP"):
            with self.subTest(provider=provider):
                machine_id, created = self.running(managed=True, provider=provider)
                cloud = self.cloud if provider == "AWS" else self.gcp
                self.assertEqual(self.complete(machine_id, created).status_code, 200)
                self.tick(machine_id)  # persist intent before cloud side effects
                self.assertTrue(self.view(machine_id)["delete_requested"])
                self.assertEqual(cloud.deleted, [])
                self.tick(machine_id)  # send cloud deletion
                self.assertEqual(len(cloud.deleted), 1)
                self.assertNotEqual(self.view(machine_id)["status"], "DELETED")
                self.tick(machine_id)  # confirm absence/termination
                self.assertEqual(self.view(machine_id)["status"], "DELETED")
                job = self.db.get(AgentJob, created["job_id"])
                self.assertEqual(job.status, "COMPLETED")
                self.assertIsNotNone(job.checkpoint_data)
                instance = self.db.scalar(
                    select(Instance).where(Instance.machine_id == machine_id)
                )
                self.assertIsNotNone(instance.ended_at)
                self.tick(machine_id)  # duplicate delivery is harmless
                self.assertEqual(len(cloud.deleted), 1)
                self.assertEqual(len(cloud.specs), 1)
                self.assertIsNone(self.db.scalar(select(RecoveryRun)))

    def test_failed_final_upload_does_not_delete_instance(self):
        machine_id, created = self.running(managed=True)
        self.assertEqual(self.complete(machine_id, created, fail=True).status_code, 503)
        self.tick(machine_id)
        self.assertFalse(self.view(machine_id)["delete_requested"])
        self.assertEqual(self.cloud.deleted, [])
        self.assertNotEqual(
            self.db.get(AgentJob, created["job_id"]).status, "COMPLETED"
        )

    def test_completion_precedes_node_timeout_and_instance_loss(self):
        machine_id, created = self.running(managed=True)
        binding = self.db.get(MachineJob, machine_id)
        self.db.get(NodeAgent, binding.active_attempt_id).last_heartbeat_at = (
            datetime.now(UTC) - timedelta(hours=1)
        )
        self.db.commit()
        self.assertEqual(self.complete(machine_id, created).status_code, 200)
        self.cloud.instances.clear()
        for _ in range(4):
            self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.assertIsNone(self.db.scalar(select(RecoveryRun)))
        self.assertEqual(self.gcp.specs, [])

    def test_completion_cancels_pending_recovery(self):
        machine_id, created = self.running(managed=True)
        self.assertEqual(self.complete(machine_id, created).status_code, 200)
        binding = self.db.get(MachineJob, machine_id)
        self.db.add(
            RecoveryRun(
                machine_id=machine_id,
                failed_attempt_id=binding.active_attempt_id,
                reason="INSTANCE_LOST",
                state="PLACEMENT",
                excluded_until=datetime.now(UTC),
            )
        )
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(self.db.scalar(select(RecoveryRun)).state, "CANCELLED")
        self.assertEqual(self.gcp.specs, [])

    def test_completion_wakes_blocked_machine_and_delete_retries_stay_bounded(self):
        from app import tasks

        machine_id, created = self.running(managed=True)
        life = self.db.get(MachineLifecycle, machine_id)
        life.blocked = True
        life.next_action_at = datetime.now(UTC) + timedelta(hours=1)
        self.db.commit()
        self.assertEqual(self.complete(machine_id, created).status_code, 200)
        with (
            patch.object(tasks, "SessionLocal") as sessions,
            patch.object(tasks.sync_machine, "apply_async") as wake,
        ):
            sessions.return_value.__enter__.return_value = self.db
            tasks.reconcile_machines.run()
            wake.assert_called_once_with(args=[machine_id], retry=False, expires=60)
        # The worker also bypasses the old recovery block/backoff once.
        fixtures.reconcile_machine(self.db, machine_id)
        self.assertTrue(life.delete_requested)
        self.assertFalse(life.blocked)
        with patch.object(self.cloud, "delete_instance", side_effect=TimeoutError):
            with self.assertLogs("app.services.instance_manager", level="ERROR"):
                for _ in range(5):
                    self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["needs_attention"])
        with (
            patch.object(tasks, "SessionLocal") as sessions,
            patch.object(tasks.sync_machine, "apply_async") as wake,
        ):
            sessions.return_value.__enter__.return_value = self.db
            tasks.reconcile_machines.run()
            wake.assert_not_called()
        self.assertEqual(self.gcp.specs, [])

    def test_temporary_delete_failure_retries_without_losing_completion(self):
        machine_id, created = self.running(managed=True)
        self.assertEqual(self.complete(machine_id, created).status_code, 200)
        self.tick(machine_id)
        with patch.object(self.cloud, "delete_instance", side_effect=TimeoutError):
            with self.assertLogs("app.services.instance_manager", level="ERROR"):
                self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["delete_requested"])
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.assertEqual(self.db.get(AgentJob, created["job_id"]).status, "COMPLETED")

    def test_unbound_completed_job_does_not_delete_ssh_machine(self):
        machine_id, _ = self.running()
        created = self.client.post(
            "/api/agent/jobs", headers=self.headers(), json={}
        ).json()
        job = self.db.get(AgentJob, created["job_id"])
        job.status = "COMPLETED"
        job.checkpoint_step = job.total_steps
        job.checkpoint_data = {"status": "COMPLETED"}
        self.db.commit()
        self.tick(machine_id)
        self.assertFalse(self.view(machine_id)["delete_requested"])
        self.assertEqual(self.cloud.deleted, [])

    def test_delete_cancels_recovery_without_replacement(self):
        machine_id, _ = self.running()
        self.cloud.instances.clear()
        self.tick(machine_id)
        response = self.client.delete(
            f"/api/machines/{machine_id}", headers=self.headers()
        )
        self.assertEqual(response.status_code, 202)
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.assertEqual(self.db.scalar(select(RecoveryRun)).state, "CANCELLED")
        self.assertEqual(self.gcp.specs, [])

    def test_cleanup_failure_never_starts_replacement(self):
        machine_id, job = self.running(managed=True)
        self.notice(job["job_id"], self.current_token(machine_id))
        self.tick(machine_id)
        with patch.object(self.cloud, "delete_instance", side_effect=TimeoutError):
            with self.assertLogs("app.services.instance_manager", level="ERROR"):
                for _ in range(5):
                    self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["needs_attention"])
        self.assertEqual(self.gcp.specs, [])

    def test_job_ownership_and_one_machine_binding(self):
        job = AgentJob(
            id=str(uuid4()),
            user_id=self.other.id,
            agent_token_hash=hashlib.sha256(b"x").hexdigest(),
            total_steps=10,
            checkpoint_every=1,
            heartbeat_every=1,
        )
        self.db.add(job)
        self.db.commit()
        self.assertEqual(self.submit(job_id=job.id).status_code, 404)
        machine_id, created = self.running(managed=True)
        self.assertEqual(self.submit(job_id=created["job_id"]).status_code, 409)
        self.assertEqual(
            self.client.post(
                f"/api/agent/jobs/{created['job_id']}/agent-token",
                headers=self.headers(),
                json={"agent_token": "x" * 40},
            ).status_code,
            409,
        )
        self.assertEqual(self.view(machine_id)["job_id"], created["job_id"])

    def test_recovery_rate_limit_requires_attention(self):
        machine_id, _ = self.running()
        self.cloud.instances.clear()
        self.tick(machine_id)
        self.tick(machine_id)
        self.tick(machine_id)
        self.gcp.instances.clear()
        with patch.dict(os.environ, {"SPULTI_RECOVERY_MAX_PER_HOUR": "1"}):
            self.tick(machine_id)
        self.assertTrue(self.view(machine_id)["needs_attention"])
        self.assertEqual(len(self.cloud.specs), 1)
