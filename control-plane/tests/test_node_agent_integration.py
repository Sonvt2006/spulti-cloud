"""True HTTP end-to-end test: real FastAPI + SQLite + two agent processes.

Run from extracted project root: python -m unittest
discover -s tests -p test_node_agent_integration.py -v
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = Path(__file__).resolve().parents[1]
AGENT = API.parent / "node-agent" / "agent.py"


def pick_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AgentHTTPTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.port = pick_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = os.environ.copy()
        env["DATABASE_URL"] = "sqlite+pysqlite:///" + str(
            Path(self.tmp.name) / "test.db"
        )
        env["JWT_SECRET"] = "node-agent-test-only-secret"
        env["TEST_PORT"] = str(self.port)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(API), str(API / "tests"), env.get("PYTHONPATH", "")]
        )
        self.proc = subprocess.Popen(
            [sys.executable, str(API / "tests" / "agent_e2e_server.py")],
            cwd=API,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_server)
        for _ in range(50):
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                self.fail(f"Server crashed: {out.decode()} {err.decode()}")
            try:
                self.request("GET", "/docs")
                break
            except URLError:
                time.sleep(0.1)
        else:
            self.fail("Server did not start")

    def stop_server(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate(timeout=5)

    def request(self, method, path, data=None, token=None, agent_token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if agent_token:
            headers["X-Agent-Token"] = agent_token
        req = Request(
            self.url + path,
            method=method,
            headers=headers,
            data=json.dumps(data).encode() if data is not None else None,
        )
        with urlopen(req, timeout=3) as r:
            return (
                json.load(r)
                if r.headers.get("Content-Type", "").startswith("application/json")
                else r.read()
            )

    def run_agent(
        self, token, job_id, state, *, crash=None, interrupt=False, checkpoint_every=10
    ):
        env = os.environ.copy()
        env["SPULTI_AGENT_TOKEN"] = token
        args = [
            sys.executable,
            str(AGENT),
            "--job-id",
            job_id,
            "--total-steps",
            "100",
            "--checkpoint-every",
            str(checkpoint_every),
            "--heartbeat-every",
            "7",
            "--step-delay",
            "0",
            "--state-dir",
            str(state),
            "--control-plane-url",
            self.url,
        ]
        if crash is not None:
            args += ["--simulate-crash-at", str(crash)]
        if interrupt:
            # Inject only the metadata source, preserving the real watcher,
            # job runner, HTTP client, API, and DB. Never contacts cloud metadata.
            fixture = """
import json
import sys
from pathlib import Path
from interruption import MetadataPoller, InterruptionNotice
from agent import main
def poll(self):
    state = Path(sys.argv[sys.argv.index('--state-dir') + 1])
    job_id = sys.argv[sys.argv.index('--job-id') + 1]
    path = state / 'heartbeats' / (job_id + '.json')
    if path.exists() and json.loads(path.read_text())['current_step'] >= 14:
        return InterruptionNotice('gcp', 'preempt')
    return None
MetadataPoller.poll = poll
raise SystemExit(main())
"""
            args = [
                sys.executable,
                "-c",
                fixture,
                *args[2:],
                "--cloud-provider",
                "gcp",
                "--interruption-poll-interval",
                "0.1",
                "--step-delay",
                "0.03",
            ]
        return subprocess.run(
            args, cwd=AGENT.parent, env=env, capture_output=True, text=True, timeout=30
        )


class ControlPlaneE2E(AgentHTTPTestCase):
    def test_crash_remote_restore_and_security(self):
        # Auth stub is deliberately limited to a single test-only owner.
        token = "test-jwt-user1"
        job = self.request(
            "POST",
            "/api/agent/jobs",
            {
                "total_steps": 100,
                "checkpoint_every": 10,
                "heartbeat_every": 7,
            },
            token=token,
        )
        job_id, agent_token = job["job_id"], job["agent_token"]
        self.assertNotEqual(token, agent_token)

        with self.assertRaises(HTTPError) as error:
            self.request(
                "GET", f"/api/agent/jobs/{job_id}/checkpoint", agent_token="bad-token"
            )
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.request("GET", f"/api/agent/jobs/{job_id}")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        vm1 = Path(self.tmp.name) / "vm1-state"
        crashed = self.run_agent(agent_token, job_id, vm1, crash=37)
        self.assertEqual(crashed.returncode, 75, crashed.stdout + crashed.stderr)
        self.assertIn(
            "CHECKPOINT step=30", crashed.stdout, crashed.stdout + crashed.stderr
        )
        saved = self.request(
            "GET", f"/api/agent/jobs/{job_id}/checkpoint", agent_token=agent_token
        )
        self.assertEqual(saved["last_completed_step"], 30)
        self.assertEqual(saved["version"], 2)
        self.assertEqual(saved["workload_state"]["running_sum"], 9455)
        stats = self.request("GET", f"/api/agent/jobs/{job_id}", token=token)
        self.assertEqual(stats["checkpoint_step"], 30)

        # A *different VM* has no local state. It must download DB checkpoint.
        vm2 = Path(self.tmp.name) / "vm2-state"
        finished = self.run_agent(agent_token, job_id, vm2)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("RESTORED_FROM_CONTROL_PLANE step=30", finished.stdout)
        self.assertIn("RESUME_FROM=30 NEXT_STEP=31", finished.stdout)
        self.assertIn("STEP=31 SQUARE=961 SUM=10416", finished.stdout)
        self.assertNotIn("STEP=1 SQUARE=", finished.stdout)
        final_checkpoint = self.request(
            "GET", f"/api/agent/jobs/{job_id}/checkpoint", agent_token=agent_token
        )
        self.assertEqual(final_checkpoint["workload_state"]["running_sum"], 338350)
        stats = self.request("GET", f"/api/agent/jobs/{job_id}", token=token)
        self.assertEqual(stats["status"], "COMPLETED")
        self.assertEqual(stats["checkpoint_step"], 100)
        self.assertTrue(stats["last_heartbeat_at"])

        # Server refuses stale or regressed checkpoints.
        with self.assertRaises(HTTPError) as error:
            self.request(
                "PUT",
                f"/api/agent/jobs/{job_id}/checkpoint",
                {
                    "version": 1,
                    "job_id": job_id,
                    "total_steps": 100,
                    "last_completed_step": 10,
                    "status": "RUNNING",
                },
                agent_token=agent_token,
            )
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

    def test_http_remote_token_forbidden(self):
        env = os.environ.copy()
        env["SPULTI_AGENT_TOKEN"] = "a" * 48
        cmd = [
            sys.executable,
            str(AGENT),
            "--job-id",
            "valid-id",
            "--control-plane-url",
            "http://example.com",
            "--state-dir",
            str(Path(self.tmp.name) / "https-validation"),
        ]
        r = subprocess.run(
            cmd, cwd=AGENT.parent, env=env, capture_output=True, text=True, timeout=8
        )
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("HTTPS", r.stderr)

    def create_demo_job(self):
        return self.request(
            "POST",
            "/api/agent/jobs",
            {"total_steps": 100, "checkpoint_every": 10, "heartbeat_every": 7},
            token="test-jwt-user1",
        )

    def test_stop_only_checkpoint_restores_step_and_result(self):
        job = self.request(
            "POST",
            "/api/agent/jobs",
            {"total_steps": 100, "heartbeat_every": 7},
            token="test-jwt-user1",
        )
        self.assertEqual(job["checkpoint_every"], 0)
        result = self.run_agent(
            job["agent_token"],
            job["job_id"],
            Path(self.tmp.name) / "first-run",
            interrupt=True,
            checkpoint_every=0,
        )
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        self.assertNotIn("[agent] CHECKPOINT step=", result.stdout)
        saved = self.request(
            "GET",
            f"/api/agent/jobs/{job['job_id']}/checkpoint",
            agent_token=job["agent_token"],
        )
        result = self.run_agent(
            job["agent_token"],
            job["job_id"],
            Path(self.tmp.name) / "resumed-run",
            checkpoint_every=0,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"RESUME_FROM={saved['last_completed_step']}", result.stdout)
        self.assertIn(f"SUM={saved['workload_state']['running_sum']}", result.stdout)
        final = self.request(
            "GET",
            f"/api/agent/jobs/{job['job_id']}/checkpoint",
            agent_token=job["agent_token"],
        )
        self.assertEqual(final["workload_state"]["running_sum"], 338350)

    def test_checkpoint_api_validates_and_roundtrips_result(self):
        job = self.create_demo_job()
        path = f"/api/agent/jobs/{job['job_id']}/checkpoint"
        checkpoint = {
            "version": 2,
            "job_id": job["job_id"],
            "total_steps": 100,
            "last_completed_step": 3,
            "status": "RUNNING",
            "workload": "sum_of_squares",
            "workload_state": {"running_sum": 14},
        }
        for state in (None, {}, {"running_sum": 13}, {"running_sum": "14"}):
            with self.subTest(state=state):
                with self.assertRaises(HTTPError) as error:
                    self.request(
                        "PUT",
                        path,
                        {**checkpoint, "workload_state": state},
                        agent_token=job["agent_token"],
                    )
                self.assertEqual(error.exception.code, 422)
                error.exception.close()
        self.request("PUT", path, checkpoint, agent_token=job["agent_token"])
        self.assertEqual(
            self.request("GET", path, agent_token=job["agent_token"]), checkpoint
        )

    def test_interruption_uploads_final_checkpoint_and_reports(self):
        job = self.create_demo_job()
        state = Path(self.tmp.name) / "spot-state"
        result = self.run_agent(
            job["agent_token"], job["job_id"], state, interrupt=True
        )
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        stats = self.request(
            "GET", f"/api/agent/jobs/{job['job_id']}", token="test-jwt-user1"
        )
        report = stats["latest_interruption"]
        self.assertEqual(stats["status"], "INTERRUPTED")
        self.assertEqual(report["provider"], "gcp")
        self.assertEqual(report["checkpoint_status"], "uploaded")
        self.assertEqual(report["current_step"], stats["checkpoint_step"])
        self.assertGreaterEqual(stats["checkpoint_step"], 14)
        self.assertLess(stats["checkpoint_step"], 100)
        saved = self.request(
            "GET",
            f"/api/agent/jobs/{job['job_id']}/checkpoint",
            agent_token=job["agent_token"],
        )
        self.assertEqual(saved["last_completed_step"], report["current_step"])
        self.assertEqual(
            saved["workload_state"]["running_sum"],
            sum(step**2 for step in range(1, report["current_step"] + 1)),
        )

    def test_interruption_is_reported_when_checkpoint_storage_fails(self):
        job = self.create_demo_job()
        self.request("POST", "/_test/fail-interrupted-checkpoint")
        state = Path(self.tmp.name) / "spot-state"
        result = self.run_agent(
            job["agent_token"], job["job_id"], state, interrupt=True
        )
        self.assertEqual(result.returncode, 130, result.stdout + result.stderr)
        stats = self.request(
            "GET", f"/api/agent/jobs/{job['job_id']}", token="test-jwt-user1"
        )
        report = stats["latest_interruption"]
        self.assertEqual(stats["status"], "INTERRUPTED")
        self.assertEqual(report["checkpoint_status"], "failed")
        self.assertEqual(report["checkpoint_error"], "upload_failed")
        self.assertEqual(report["last_checkpoint_step"], stats["checkpoint_step"])
        self.assertGreater(report["current_step"], stats["checkpoint_step"])
        saved = self.request(
            "GET",
            f"/api/agent/jobs/{job['job_id']}/checkpoint",
            agent_token=job["agent_token"],
        )
        self.assertEqual(saved["status"], "RUNNING")
        local = json.loads(
            (state / "interruptions" / f"{job['job_id']}.json").read_text()
        )
        self.assertTrue(local["reported"])

    def test_interruption_auth_validation_and_retry_after_completion(self):
        from uuid import uuid4

        job = self.create_demo_job()
        path = f"/api/agent/jobs/{job['job_id']}/interruptions"
        report = {
            "event_id": str(uuid4()),
            "provider": "aws",
            "action": "terminate",
            "detected_at": datetime.now(UTC).isoformat(),
            "current_step": 0,
            "last_checkpoint_step": 0,
            "local_checkpoint_step": 0,
            "checkpoint_status": "failed",
            "checkpoint_error": "upload_failed",
        }
        with self.assertRaises(HTTPError) as error:
            self.request("POST", path, report, agent_token="wrong-token")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        for changes, status in [
            ({"action": "preempt"}, 422),
            ({"current_step": 101}, 422),
            ({"checkpoint_status": "uploaded", "checkpoint_error": None}, 409),
            ({"current_step": 10, "last_checkpoint_step": 10}, 409),
        ]:
            with self.subTest(changes=changes):
                with self.assertRaises(HTTPError) as error:
                    self.request(
                        "POST",
                        path,
                        {**report, **changes},
                        agent_token=job["agent_token"],
                    )
                self.assertEqual(error.exception.code, status)
                error.exception.close()
        first = self.request("POST", path, report, agent_token=job["agent_token"])
        self.assertEqual(first["status"], "ack")
        finished = self.run_agent(
            job["agent_token"], job["job_id"], Path(self.tmp.name) / "completed"
        )
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        retry = self.request("POST", path, report, agent_token=job["agent_token"])
        self.assertEqual(first, retry)
        stats = self.request(
            "GET", f"/api/agent/jobs/{job['job_id']}", token="test-jwt-user1"
        )
        self.assertEqual(stats["status"], "COMPLETED")
        with self.assertRaises(HTTPError) as error:
            self.request(
                "POST",
                path,
                {**report, "current_step": 1},
                agent_token=job["agent_token"],
            )
        self.assertEqual(error.exception.code, 409)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
