"""Managed dashboard submissions use isolated DB and cloud fixtures."""

import hashlib
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_instance_manager as fixtures
from app.dashboard import router
from app.models import AgentJob, AgentProgram, InstanceAttempt, Machine, MachineJob
from app.services.managed_agent import attempt_token
from kombu.exceptions import OperationalError
from sqlalchemy import func, select


class DashboardJobTests(unittest.TestCase):
    tearDown = fixtures.InstanceManagerTests.tearDown
    headers = fixtures.InstanceManagerTests.headers
    tick = fixtures.InstanceManagerTests.tick

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

    def submit_job(self, key=None, user=None, **changes):
        payload = {"filename": "training.py", "source_code": "print('hello')\n"}
        payload.update(changes)
        return self.client.post(
            "/api/dashboard/jobs", headers=self.headers(user, key), json=payload
        )

    def assert_empty(self):
        for model in (AgentJob, AgentProgram, Machine, MachineJob, InstanceAttempt):
            self.assertEqual(self.db.scalar(select(func.count()).select_from(model)), 0)

    def test_one_route_and_auth_required(self):
        routes = [
            r
            for r in router.routes
            if getattr(r, "path", None) == "/api/dashboard/jobs"
        ]
        self.assertEqual(len(routes), 1)
        self.assertEqual(
            self.client.post("/api/dashboard/jobs", json={}).status_code, 401
        )

    def test_options_handles_valid_and_missing_agent_configuration(self):
        cases = [
            ({}, True),
            ({"SPULTI_PUBLIC_API_URL": ""}, False),
            ({"SPULTI_PUBLIC_API_URL": "https://api.example.test?query=1"}, False),
            ({"SPULTI_AGENT_RELEASE_URL": ""}, False),
            ({"SPULTI_AGENT_RELEASE_SHA256": "invalid"}, False),
        ]
        for config, configured in cases:
            with self.subTest(config=config), patch.dict(os.environ, config):
                response = self.client.get(
                    "/api/dashboard/options", headers=self.headers()
                )
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data["managed_agent_configured"], configured)
                self.assertEqual(data["managed_agent_error"] is None, configured)
                self.assertTrue(data["locations"])

    def test_program_is_stored_and_delivered_to_managed_attempt(self):
        # Compilation during upload must never execute user code on the API.
        source = "raise RuntimeError('only the VM should run this')\n"
        response = self.submit_job(source_code=source)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        job = self.db.get(AgentJob, data["job_id"])
        program = self.db.get(AgentProgram, job.id)
        self.assertEqual(job.user_id, self.owner.id)
        self.assertEqual(job.total_steps, 1)
        self.assertEqual(float(program.step_delay), 0)
        self.assertEqual(
            program.source_sha256, hashlib.sha256(source.encode()).hexdigest()
        )
        self.assertEqual(data["machine"]["job_id"], job.id)
        self.assertEqual(data["candidate"]["provider"], "AWS")
        self.assertEqual(data["candidate"]["spot_price_per_hour"], "0.100000")
        for private in ("source_code", "agent_token", "adapter_config", "spec"):
            self.assertNotIn(private, response.text)
        self.tick(data["machine"]["id"])
        self.tick(data["machine"]["id"])
        binding = self.db.get(MachineJob, data["machine"]["id"])
        attempt = self.db.get(InstanceAttempt, binding.active_attempt_id)
        config = self.client.get(
            f"/api/agent/jobs/{job.id}/config",
            headers={"X-Agent-Token": attempt_token(attempt.request_id)},
        )
        self.assertEqual(config.status_code, 200, config.text)
        self.assertEqual(config.json()["program"]["source_code"], source)
        self.assertEqual(
            self.client.get(
                f"/api/dashboard/machines/{data['machine']['id']}",
                headers=self.headers(self.other),
            ).status_code,
            404,
        )

    def test_provider_resources_and_allowlist(self):
        response = self.submit_job(provider="GCP", vcpu=4, ram_gib=16, total_steps=100)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["candidate"]["provider"], "GCP")
        self.assertEqual(data["machine"]["requested_vcpu"], 4)
        self.assertEqual(self.db.get(AgentJob, data["job_id"]).total_steps, 100)
        attempts = self.db.scalars(select(InstanceAttempt)).all()
        self.assertTrue(all(a.provider == "GCP" for a in attempts))

    def test_retry_reuses_job_and_machine_even_after_catalog_is_unavailable(self):
        key = str(uuid4())
        first = self.submit_job(key=key)
        self.assertEqual(first.status_code, 201, first.text)
        with patch.dict(os.environ, {"SPULTI_LAUNCH_CONFIG": "[]"}):
            second = self.submit_job(key=key)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(first.json()["job_id"], second.json()["job_id"])
        self.assertEqual(first.json()["machine"]["id"], second.json()["machine"]["id"])
        self.assertEqual(first.json()["candidate"], second.json()["candidate"])
        for model in (AgentJob, AgentProgram, Machine, MachineJob):
            self.assertEqual(self.db.scalar(select(func.count()).select_from(model)), 1)

    def test_reuse_with_changed_payload_conflicts(self):
        key = str(uuid4())
        self.assertEqual(self.submit_job(key=key).status_code, 201)
        for changes in (
            {"source_code": "print(2)"},
            {"total_steps": 2},
            {"filename": "other.py"},
            {"checkpoint_every": 0},
            {"step_delay": 1},
            {"provider": "GCP"},
            {"vcpu": 4},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.submit_job(key=key, **changes).status_code, 409)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AgentJob)), 1)

    def test_validation_rejects_bad_source_names_sizes_and_settings(self):
        for changes in (
            {"filename": "../bad.py"},
            {"filename": "bad.txt"},
            {"source_code": "def broken(:"},
            {"source_code": ""},
            {"source_code": "#" + "あ" * 6000},
            {"total_steps": 0},
            {"vcpu": 0},
            {"ram_gib": 0},
            {"unexpected": True},
        ):
            with self.subTest(changes=list(changes)):
                self.assertEqual(self.submit_job(**changes).status_code, 422)
        self.assert_empty()

    def test_failures_roll_back_program_and_job(self):
        for config in ("[]", "not json"):
            with patch.dict(os.environ, {"SPULTI_LAUNCH_CONFIG": config}):
                self.assertEqual(self.submit_job().status_code, 503)
            self.assert_empty()
        self.assertEqual(self.submit_job(vcpu=99999).status_code, 409)
        self.assert_empty()
        self.assertEqual(self.submit_job(user=self.other).status_code, 422)
        self.assert_empty()
        with patch.dict(os.environ, {"SPULTI_PUBLIC_API_URL": ""}):
            self.assertEqual(self.submit_job().status_code, 503)
        self.assert_empty()

    def test_quota_failure_does_not_leave_orphan_job(self):
        with patch.dict(os.environ, {"SPULTI_MAX_MACHINES_PER_USER": "1"}):
            self.assertEqual(self.submit_job().status_code, 201)
            self.assertEqual(self.submit_job().status_code, 409)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AgentJob)), 1)

    def test_redis_outage_keeps_intent_for_beat(self):
        self.wake.side_effect = OperationalError("offline")
        response = self.submit_job()
        self.assertEqual(response.status_code, 201, response.text)
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(Machine, response.json()["machine"]["id"]))


if __name__ == "__main__":
    unittest.main()
