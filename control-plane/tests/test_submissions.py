"""User SSH workflow over real HTTP, with no VM or cloud dependencies."""

import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from uuid import uuid4

from test_node_agent_integration import AGENT, AgentHTTPTestCase

SOURCE = """from spulti import state, step_number
print('USER_MODULE_LOADED', flush=True)
state['running_sum'] = state.get('running_sum', 0) + step_number ** 2
print(f"USER_CODE step={step_number} sum={state['running_sum']}", flush=True)
"""

INTERRUPT_AFTER_FOUR = """
import signal
from heartbeat import HeartbeatStore
from agent import main
original = HeartbeatStore.write
def interrupt(self, step, saved, status):
    original(self, step, saved, status)
    if step == 4 and status == 'RUNNING':
        signal.raise_signal(signal.SIGTERM)
HeartbeatStore.write = interrupt
raise SystemExit(main())
"""


class SubmissionE2E(AgentHTTPTestCase):
    def manifest(self, state, stdout):
        job_id = re.search(r"JOB_ID=([a-f0-9-]{36})", stdout).group(1)
        return json.loads((state / "jobs" / f"{job_id}.json").read_text())

    def run_command(self, *args, script=None):
        env = os.environ.copy()
        for name in ("SPULTI_CONTROL_PLANE_URL", "SPULTI_AGENT_TOKEN"):
            env.pop(name, None)
        env.update(
            {
                "SPULTI_API_URL": self.url,
                "SPULTI_API_KEY": "test-jwt-user1",
                "SPULTI_CLOUD_PROVIDER": "none",
                "SPULTI_CONFIG": str(Path(self.tmp.name) / "unused-cli-config.json"),
            }
        )
        command = (
            [sys.executable, str(AGENT)]
            if script is None
            else [sys.executable, "-c", script]
        )
        return subprocess.run(
            [*command, *args],
            env=env,
            cwd=AGENT.parent,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_submit_code_interrupt_and_resume_by_id_on_fresh_vm(self):
        root = Path(self.tmp.name)
        code = root / "my_squares.py"
        code.write_text(SOURCE)
        first = self.run_command(
            "submit",
            str(code),
            "--total-steps",
            "6",
            "--step-delay",
            "0",
            "--state-dir",
            str(root / "vm1"),
            script=INTERRUPT_AFTER_FOUR,
        )
        self.assertEqual(first.returncode, 130, first.stdout + first.stderr)
        job_id = re.search(r"JOB_ID=([a-f0-9-]{36})", first.stdout).group(1)
        manifest = json.loads((root / "vm1/jobs" / f"{job_id}.json").read_text())
        old_token = manifest["agent_token"]
        self.assertNotIn(old_token, first.stdout + first.stderr)
        self.assertIn("USER_CODE step=4 sum=30", first.stdout)
        self.assertNotIn("[agent] CHECKPOINT step=", first.stdout)
        saved = self.request(
            "GET", f"/api/agent/jobs/{job_id}/checkpoint", agent_token=old_token
        )
        self.assertEqual(saved["last_completed_step"], 4)
        self.assertEqual(saved["workload_state"]["running_sum"], 30)
        code.unlink()  # Fresh VM has neither source nor local checkpoints/token.
        resumed = self.run_command("resume", job_id, "--state-dir", str(root / "vm2"))
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("RESTORED_FROM_CONTROL_PLANE step=4", resumed.stdout)
        self.assertIn("USER_CODE step=5 sum=55", resumed.stdout)
        self.assertNotIn("USER_CODE step=1 ", resumed.stdout)
        stats = self.request("GET", f"/api/agent/jobs/{job_id}", token="test-jwt-user1")
        self.assertEqual(stats["status"], "COMPLETED")
        new_token = json.loads((root / "vm2/jobs" / f"{job_id}.json").read_text())[
            "agent_token"
        ]
        saved = self.request(
            "GET", f"/api/agent/jobs/{job_id}/checkpoint", agent_token=new_token
        )
        self.assertEqual(saved["workload_state"]["running_sum"], 91)
        with self.assertRaises(HTTPError) as error:
            self.request(
                "GET", f"/api/agent/jobs/{job_id}/config", agent_token=old_token
            )
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        completed = self.run_command("resume", job_id, "--state-dir", str(root / "vm2"))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("already COMPLETED", completed.stdout)
        self.assertNotIn("USER_MODULE_LOADED", completed.stdout)

    def test_managed_vm_restores_program_and_checkpoint_without_owner_credentials(self):
        root = Path(self.tmp.name)
        code = root / "managed_squares.py"
        code.write_text(SOURCE)
        first = self.run_command(
            "submit",
            str(code),
            "--total-steps",
            "6",
            "--step-delay",
            "0",
            "--state-dir",
            str(root / "old-vm"),
            script=INTERRUPT_AFTER_FOUR,
        )
        self.assertEqual(first.returncode, 130, first.stdout + first.stderr)
        manifest = self.manifest(root / "old-vm", first.stdout)
        job_id = manifest["job_id"]
        new_token = "replacement-attempt-token-" + uuid4().hex
        self.request(
            "POST",
            f"/api/agent/jobs/{job_id}/agent-token",
            {"agent_token": new_token},
            token="test-jwt-user1",
        )
        code.unlink()
        env = os.environ.copy()
        env.pop("SPULTI_API_KEY", None)
        env.update(
            SPULTI_JOB_ID=job_id,
            SPULTI_AGENT_TOKEN=new_token,
            SPULTI_CONTROL_PLANE_URL=self.url,
            SPULTI_CLOUD_PROVIDER="none",
        )
        command = [
            sys.executable,
            str(AGENT),
            "managed",
            "--state-dir",
            str(root / "new-vm"),
        ]
        resumed = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("RESTORED_FROM_CONTROL_PLANE step=4", resumed.stdout)
        self.assertIn("USER_CODE step=6 sum=91", resumed.stdout)
        self.assertNotIn("USER_CODE step=1", resumed.stdout)
        self.assertNotIn(new_token, resumed.stdout + resumed.stderr)
        repeated = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertNotIn("USER_CODE", repeated.stdout)

    def test_lost_registration_reply_reuses_same_job(self):
        root = Path(self.tmp.name)
        code = root / "squares.py"
        code.write_text("print('hello spulti')\n")
        fixture = """
from submission import OwnerClient
from control_plane import RemoteError
from agent import main
original = OwnerClient.request
def lose_reply(self, method, path, payload=None):
    result = original(self, method, path, payload)
    if method == 'PUT':
        raise RemoteError('Simulated lost registration reply')
    return result
OwnerClient.request = lose_reply
raise SystemExit(main())
"""
        failed = self.run_command(
            "submit",
            str(code),
            "--total-steps",
            "3",
            "--step-delay",
            "0",
            "--state-dir",
            str(root / "state"),
            script=fixture,
        )
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        job_id = re.search(r"JOB_ID=([a-f0-9-]{36})", failed.stdout).group(1)
        resumed = self.run_command("resume", job_id, "--state-dir", str(root / "state"))
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        with closing(sqlite3.connect(root / "test.db")) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM agent_jobs").fetchone()[0], 1
            )

    def test_submission_ownership_idempotency_and_server_never_executes_code(self):
        job_id = str(uuid4())
        path = f"/api/agent/submissions/{job_id}"
        body = {
            "agent_token": "a" * 48,
            "filename": "squares.py",
            "total_steps": 5,
            "source_code": (
                "raise RuntimeError('server must not execute')\n"
                "def step(n, s): return s+n*n\n"
            ),
        }
        with self.assertRaises(HTTPError) as error:
            self.request("PUT", path, body)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        first = self.request("PUT", path, body, token="test-jwt-user1")
        second = self.request("PUT", path, body, token="test-jwt-user1")
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["checkpoint_every"], 0)
        self.assertEqual(first["step_delay"], 5)
        self.assertNotIn("agent_token", first)
        checks = [
            (
                "GET",
                f"/api/agent/jobs/{job_id}/submission",
                None,
                "test-jwt-user2",
                404,
            ),
            (
                "POST",
                f"/api/agent/jobs/{job_id}/agent-token",
                {"agent_token": "b" * 48},
                "test-jwt-user2",
                404,
            ),
            ("PUT", path, body, "test-jwt-user2", 409),
            ("PUT", path, {**body, "total_steps": 6}, "test-jwt-user1", 409),
            (
                "PUT",
                f"/api/agent/submissions/{uuid4()}",
                {**body, "source_code": "def broken(:"},
                "test-jwt-user1",
                422,
            ),
        ]
        for method, target, payload, token, status in checks:
            with self.subTest(method=method, status=status):
                with self.assertRaises(HTTPError) as error:
                    self.request(method, target, payload, token=token)
                self.assertEqual(error.exception.code, status)
                error.exception.close()

    def test_plain_python_print_runs_exactly_100_times(self):
        root = Path(self.tmp.name)
        code = root / "hello.py"
        code.write_text("if __name__ == '__main__':\n    print('hello spulti')\n")
        result = self.run_command(
            "submit",
            str(code),
            "--total-steps",
            "100",
            "--step-delay",
            "0",
            "--state-dir",
            str(root / "state"),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines().count("hello spulti"), 100)
        manifest = self.manifest(root / "state", result.stdout)
        checkpoint = self.request(
            "GET",
            f"/api/agent/jobs/{manifest['job_id']}/checkpoint",
            agent_token=manifest["agent_token"],
        )
        self.assertEqual(checkpoint["version"], 3)
        self.assertEqual(checkpoint["last_completed_step"], 100)
        self.assertEqual(checkpoint["workload_state"], {})
        self.assertEqual(checkpoint["status"], "COMPLETED")

    def test_compile_error_reports_line_without_execution_or_registration(self):
        root = Path(self.tmp.name)
        marker = root / "should-not-exist"
        code = root / "broken.py"
        code.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
            "def broken(:\n    pass\n"
        )
        result = self.run_command(
            "submit",
            str(code),
            "--total-steps",
            "100",
            "--step-delay",
            "0",
            "--state-dir",
            str(root / "state"),
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("syntax error at line 3", result.stderr)
        self.assertFalse(marker.exists())
        self.assertNotIn("REGISTERING", result.stdout)
        with closing(sqlite3.connect(root / "test.db")) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM agent_jobs").fetchone()[0], 0
            )

    def test_timed_checkpoint_recovers_without_any_interruption_notice(self):
        root = Path(self.tmp.name)
        code = root / "squares.py"
        code.write_text(
            "import time\nfrom spulti import state, step_number\n"
            "state['sum'] = state.get('sum', 0) + step_number ** 2\n"
            "time.sleep(0.25)\n"
            "print(f'FINISHED_{step_number}')\n"
        )
        fixture = """
from dataclasses import replace
import submission
from agent import main
original = submission.run_job
def crash(config, **kwargs):
    return original(replace(config, simulate_crash_at=4), **kwargs)
submission.run_job = crash
raise SystemExit(main())
"""
        failed = self.run_command(
            "submit",
            str(code),
            "--total-steps",
            "6",
            "--step-delay",
            "0",
            "--checkpoint-seconds",
            "0.1",
            "--state-dir",
            str(root / "vm1"),
            script=fixture,
        )
        self.assertEqual(failed.returncode, 75, failed.stdout + failed.stderr)
        manifest = self.manifest(root / "vm1", failed.stdout)
        job_id = manifest["job_id"]
        saved = self.request(
            "GET",
            f"/api/agent/jobs/{job_id}/checkpoint",
            agent_token=manifest["agent_token"],
        )
        self.assertEqual(saved["last_completed_step"], 3)
        self.assertEqual(saved["workload_state"], {"sum": 14})
        self.assertEqual(saved["status"], "RUNNING")
        with closing(sqlite3.connect(root / "test.db")) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM agent_interruptions").fetchone()[0], 0
            )
        code.unlink()
        recovered = self.run_command("resume", job_id, "--state-dir", str(root / "vm2"))
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertIn("RESUME_FROM=3 NEXT_STEP=4", recovered.stdout)
        self.assertNotIn("FINISHED_1", recovered.stdout)
        self.assertIn("FINISHED_4", recovered.stdout)
        final = json.loads((root / "vm2/checkpoints" / f"{job_id}.json").read_text())
        self.assertEqual(final["last_completed_step"], 6)
        self.assertEqual(final["workload_state"], {"sum": 91})

    def test_runtime_failure_keeps_successful_state_and_can_resume(self):
        root = Path(self.tmp.name)
        fail_flag = root / "fail-on-third"
        fail_flag.touch()
        code = root / "product.py"
        code.write_text(
            "from pathlib import Path\n"
            "from spulti import state, step_number\n"
            "state['product'] = state.get('product', 1) * step_number\n"
            "state['extra'] = [None, True, {'value': 'hello'}]\n"
            f"if step_number == 3 and Path({str(fail_flag)!r}).exists():\n"
            "    raise RuntimeError('intentional failure')\n"
            "print(f'DONE_{step_number}')\n"
        )
        failed = self.run_command(
            "submit",
            str(code),
            "--total-steps",
            "5",
            "--step-delay",
            "0",
            "--state-dir",
            str(root / "vm1"),
        )
        self.assertEqual(failed.returncode, 2, failed.stdout + failed.stderr)
        self.assertIn("intentional failure", failed.stderr)
        self.assertNotIn("DONE_3", failed.stdout)
        self.assertNotIn("STEP=3", failed.stdout)
        manifest = self.manifest(root / "vm1", failed.stdout)
        job_id = manifest["job_id"]
        saved = self.request(
            "GET",
            f"/api/agent/jobs/{job_id}/checkpoint",
            agent_token=manifest["agent_token"],
        )
        self.assertEqual(saved["last_completed_step"], 2)
        self.assertEqual(
            saved["workload_state"],
            {
                "product": 2,
                "extra": [None, True, {"value": "hello"}],
            },
        )
        # Checkpoint state is generic, but is still bound to the saved source.
        for invalid in (
            {**saved, "source_sha256": "0" * 64},
            {**saved, "workload_state": []},
        ):
            with self.assertRaises(HTTPError) as error:
                self.request(
                    "PUT",
                    f"/api/agent/jobs/{job_id}/checkpoint",
                    invalid,
                    agent_token=manifest["agent_token"],
                )
            self.assertEqual(error.exception.code, 422)
            error.exception.close()
        fail_flag.unlink()
        code.unlink()
        resumed = self.run_command("resume", job_id, "--state-dir", str(root / "vm2"))
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
        self.assertIn("DONE_3", resumed.stdout)
        self.assertNotIn("DONE_1", resumed.stdout)
        local = json.loads((root / "vm2/checkpoints" / f"{job_id}.json").read_text())
        self.assertEqual(local["workload_state"]["product"], 120)
