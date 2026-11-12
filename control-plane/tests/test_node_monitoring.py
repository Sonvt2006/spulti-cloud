"""Node recovery with no job, attempt fencing, and independent job completion."""

import base64
import re
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import test_instance_manager as fixtures
from app.models import AgentJob, InstanceAttempt, NodeAgent, RecoveryRun
from app.services.managed_agent import attempt_token, node_token
from kombu.exceptions import OperationalError
from sqlalchemy import func, select


class NodeMonitoringTests(unittest.TestCase):
    setUp = fixtures.InstanceManagerTests.setUp
    tearDown = fixtures.InstanceManagerTests.tearDown
    headers = fixtures.InstanceManagerTests.headers
    submit = fixtures.InstanceManagerTests.submit
    create = fixtures.InstanceManagerTests.create
    tick = fixtures.InstanceManagerTests.tick
    view = fixtures.InstanceManagerTests.view
    provision = fixtures.InstanceManagerTests.provision

    def active(self, machine_id):
        self.db.expire_all()
        return self.db.scalar(
            select(InstanceAttempt).where(
                InstanceAttempt.machine_id == machine_id,
                InstanceAttempt.state == "ACTIVE",
            )
        )

    def heartbeat(self, attempt, token=None):
        return self.client.post(
            f"/api/agent/nodes/{attempt.request_id}/heartbeat",
            headers={"X-Agent-Token": token or node_token(attempt.request_id)},
            json={},
        )

    def running(self, **kwargs):
        machine_id = self.provision(**kwargs)
        key = self.cloud.specs[-1].request_id
        self.cloud.instances[key] = replace(self.cloud.instances[key], status="running")
        self.tick(machine_id)
        attempt = self.active(machine_id)
        self.assertEqual(self.heartbeat(attempt).status_code, 200)
        return machine_id, attempt

    def notice(self, attempt, payload=None):
        payload = payload or {
            "event_id": str(uuid4()),
            "provider": attempt.provider.lower(),
            "action": "terminate" if attempt.provider == "AWS" else "preempt",
            "detected_at": datetime.now(UTC).isoformat(),
            "deadline_at": None,
        }
        return self.client.post(
            f"/api/agent/nodes/{attempt.request_id}/interruptions",
            headers={"X-Agent-Token": node_token(attempt.request_id)},
            json=payload,
        )

    def test_bootstrap_starts_monitor_without_job_and_uses_separate_token(self):
        machine_id, attempt = self.running()
        script = attempt.spec["startup_script"]
        encoded = re.search(
            r"printf %s (\S+) \| base64 -d > /etc/spulti-node.env", script
        )[1]
        config = base64.b64decode(encoded).decode()
        self.assertIn(node_token(attempt.request_id), config)
        self.assertIn(attempt.request_id, config)
        self.assertNotIn("SPULTI_JOB_ID", config)
        self.assertIn("agent.py daemon", script)
        self.assertIn("Environment=SPULTI_CLOUD_PROVIDER=aws", script)
        self.assertIn("systemctl enable --now spulti-node-agent.service", script)
        self.assertNotEqual(
            node_token(attempt.request_id), attempt_token(attempt.request_id)
        )
        self.assertEqual(self.view(machine_id)["node_status"], "ONLINE")
        self.assertEqual(self.db.scalar(select(func.count()).select_from(AgentJob)), 0)

    def test_node_requires_https_api_even_without_job(self):
        with patch.dict("os.environ", {"SPULTI_PUBLIC_API_URL": ""}):
            response = self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.cloud.specs, [])

    def test_managed_job_and_node_use_separate_services_and_credentials(self):
        job = self.client.post(
            "/api/agent/jobs", headers=self.headers(), json={}
        ).json()
        machine_id, attempt = self.running(
            job_id=job["job_id"], startup_script="#!/bin/bash\necho prepare-job\nexit 0"
        )
        script = attempt.spec["startup_script"]
        self.assertLess(
            script.index("enable --now spulti-node-agent"),
            script.index("echo prepare-job"),
        )
        self.assertLess(
            script.index("echo prepare-job"),
            script.index("systemctl start spulti-job-agent"),
        )
        self.assertIn("Restart=always", script)
        endpoint = f"/api/agent/jobs/{job['job_id']}/heartbeat"
        payload = {"current_step": 0, "last_checkpoint_step": 0, "status": "RUNNING"}
        self.assertEqual(
            self.client.post(
                endpoint,
                json=payload,
                headers={
                    "X-Agent-Token": node_token(attempt.request_id),
                },
            ).status_code,
            401,
        )
        self.assertEqual(self.notice(attempt).status_code, 200)
        self.tick(machine_id)
        self.assertEqual(
            self.client.post(
                endpoint,
                json=payload,
                headers={
                    "X-Agent-Token": attempt_token(attempt.request_id),
                },
            ).status_code,
            200,
        )  # final checkpoint credentials remain valid in grace

    def test_node_can_report_while_cloud_create_is_still_returning(self):
        responses = []

        def on_create(spec):
            attempt = self.db.scalar(
                select(InstanceAttempt).where(
                    InstanceAttempt.request_id == spec.request_id,
                )
            )
            self.assertEqual(attempt.state, "CREATING")
            responses.append(self.heartbeat(attempt).status_code)

        self.cloud.on_create = on_create
        self.provision()
        self.assertEqual(responses, [200])

    def test_tokens_are_attempt_scoped_and_inactive_before_dispatch(self):
        machine_id = self.create()
        attempts = list(self.db.scalars(select(InstanceAttempt)))
        self.assertEqual(self.heartbeat(attempts[0]).status_code, 401)
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.heartbeat(attempts[0], "wrong" * 10).status_code, 401)
        self.assertEqual(
            self.heartbeat(attempts[1], node_token(attempts[0].request_id)).status_code,
            401,
        )
        self.assertEqual(
            self.heartbeat(
                attempts[0], attempt_token(attempts[0].request_id)
            ).status_code,
            401,
        )
        self.assertEqual(self.heartbeat(attempts[0]).status_code, 200)

    def test_notice_recovers_idle_machine_and_requires_replacement_heartbeat(self):
        machine_id, old = self.running()
        self.assertEqual(self.notice(old).status_code, 200)
        self.db.expire_all()
        node = self.db.get(NodeAgent, old.id)
        report = node.interruption.copy()
        deadline = node.recovery_after
        self.assertEqual(self.notice(old, report).status_code, 200)
        self.assertEqual(self.db.get(NodeAgent, old.id).recovery_after, deadline)
        self.assertEqual(self.notice(old).status_code, 409)
        self.tick(machine_id)
        self.assertIsNone(self.db.scalar(select(RecoveryRun)))  # checkpoint grace
        node.recovery_after = datetime.now(UTC) - timedelta(seconds=1)
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(self.heartbeat(old).status_code, 401)
        self.tick(machine_id)  # delete old VM
        self.assertEqual(self.gcp.specs, [])
        self.tick(machine_id)  # confirm absent
        self.tick(machine_id)  # place
        self.tick(machine_id)  # create replacement
        new = self.active(machine_id)
        self.assertEqual(new.provider, "GCP")
        self.assertIn("SPULTI_CLOUD_PROVIDER=gcp", new.spec["startup_script"])
        self.assertIn("agent.py daemon", new.spec["startup_script"])
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "RECOVERING")
        self.assertEqual(self.heartbeat(new).status_code, 200)
        self.tick(machine_id)
        run = self.db.scalar(select(RecoveryRun))
        self.assertEqual((run.reason, run.state), ("SPOT_NOTICE", "SUCCEEDED"))
        self.assertEqual(self.view(machine_id)["status"], "RUNNING")
        self.assertEqual(self.heartbeat(old).status_code, 401)
        # Monitoring continues on the replacement, not only on the first VM.
        self.assertEqual(self.notice(new).status_code, 200)
        node = self.db.get(NodeAgent, new.id, populate_existing=True)
        node.recovery_after = datetime.now(UTC) - timedelta(seconds=1)
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(RecoveryRun)), 2
        )

    def test_missing_and_stale_node_heartbeat_recover_without_job(self):
        for received, reason in (
            (False, "NODE_BOOT_TIMEOUT"),
            (True, "NODE_HEARTBEAT_TIMEOUT"),
        ):
            with self.subTest(received=received):
                machine_id, attempt = self.running()
                node = self.db.get(NodeAgent, attempt.id, populate_existing=True)
                node.activated_at = datetime.now(UTC) - timedelta(seconds=601)
                node.last_heartbeat_at = (
                    datetime.now(UTC) - timedelta(seconds=91) if received else None
                )
                self.db.commit()
                self.tick(machine_id)
                run = self.db.scalar(
                    select(RecoveryRun).where(RecoveryRun.machine_id == machine_id)
                )
                self.assertEqual(run.reason, reason)

    def test_completed_job_does_not_disable_node_monitoring(self):
        created = self.client.post(
            "/api/agent/jobs", headers=self.headers(), json={}
        ).json()
        machine_id, attempt = self.running(job_id=created["job_id"])
        self.db.get(AgentJob, created["job_id"]).status = "COMPLETED"
        node = self.db.get(NodeAgent, attempt.id, populate_existing=True)
        node.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=91)
        self.db.commit()
        self.tick(machine_id)
        self.assertEqual(
            self.db.scalar(select(RecoveryRun)).reason, "NODE_HEARTBEAT_TIMEOUT"
        )

    def test_notice_survives_redis_outage_and_delete_prevents_replacement(self):
        machine_id, attempt = self.running()
        self.wake.side_effect = OperationalError("offline")
        self.assertEqual(self.notice(attempt).status_code, 200)
        self.assertIsNotNone(
            self.db.get(NodeAgent, attempt.id, populate_existing=True).interruption
        )
        self.client.delete(f"/api/machines/{machine_id}", headers=self.headers())
        self.assertEqual(self.heartbeat(attempt).status_code, 401)
        self.tick(machine_id)
        self.tick(machine_id)
        self.assertEqual(self.view(machine_id)["status"], "DELETED")
        self.assertEqual(self.gcp.specs, [])

    def test_rejects_wrong_provider_and_local_stop_notice(self):
        _, attempt = self.running()
        report = {
            "event_id": str(uuid4()),
            "provider": "gcp",
            "action": "preempt",
            "detected_at": datetime.now(UTC).isoformat(),
        }
        self.assertEqual(self.notice(attempt, report).status_code, 422)
        report.update(provider="signal", action="sigterm")
        self.assertEqual(self.notice(attempt, report).status_code, 422)
