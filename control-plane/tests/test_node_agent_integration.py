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
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = Path(__file__).resolve().parents[1]
AGENT = API.parent / "node-agent" / "agent.py"


def pick_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ControlPlaneE2E(unittest.TestCase):
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

    def run_agent(self, token, job_id, state, *, crash=None):
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
            "10",
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
        return subprocess.run(
            args, cwd=AGENT.parent, env=env, capture_output=True, text=True, timeout=30
        )

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
        stats = self.request("GET", f"/api/agent/jobs/{job_id}", token=token)
        self.assertEqual(stats["checkpoint_step"], 30)

        # A *different VM* has no local state. It must download DB checkpoint.
        vm2 = Path(self.tmp.name) / "vm2-state"
        finished = self.run_agent(agent_token, job_id, vm2)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        self.assertIn("RESTORED_FROM_CONTROL_PLANE step=30", finished.stdout)
        self.assertIn("RESUME_FROM=30 NEXT_STEP=31", finished.stdout)
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
        ]
        r = subprocess.run(
            cmd, cwd=AGENT.parent, env=env, capture_output=True, text=True, timeout=8
        )
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("HTTPS", r.stderr)


if __name__ == "__main__":
    unittest.main()
