"""Run with: python -m unittest discover -s tests -v"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "agent.py"


class NodeAgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"

    def run_agent(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                str(AGENT),
                "--job-id",
                "test-job",
                "--total-steps",
                "100",
                "--checkpoint-every",
                "10",
                "--heartbeat-every",
                "7",
                "--step-delay",
                "0",
                "--state-dir",
                str(self.state),
                *extra,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def read_checkpoint(self):
        return json.loads((self.state / "checkpoints" / "test-job.json").read_text())

    def read_heartbeat(self):
        return json.loads((self.state / "heartbeats" / "test-job.json").read_text())

    def test_fresh_complete_then_idempotent(self):
        first = self.run_agent()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.read_checkpoint()["status"], "COMPLETED")
        self.assertEqual(self.read_checkpoint()["last_completed_step"], 100)
        second = self.run_agent()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already COMPLETED", second.stdout)

    def test_hard_crash_then_resume_from_last_checkpoint(self):
        crash = self.run_agent("--simulate-crash-at", "37")
        self.assertEqual(crash.returncode, 75, crash.stdout + crash.stderr)
        self.assertEqual(self.read_checkpoint()["last_completed_step"], 30)
        self.assertEqual(self.read_checkpoint()["status"], "RUNNING")
        recovery = self.run_agent()
        self.assertEqual(recovery.returncode, 0, recovery.stderr)
        self.assertIn("RESUME_FROM=30 NEXT_STEP=31", recovery.stdout)
        self.assertEqual(self.read_checkpoint()["last_completed_step"], 100)
        self.assertEqual(self.read_heartbeat()["status"], "COMPLETED")
        self.assertEqual(self.read_heartbeat()["last_checkpoint_step"], 100)

    def test_total_steps_mismatch_does_not_overwrite_checkpoint(self):
        self.assertEqual(self.run_agent("--simulate-crash-at", "25").returncode, 75)
        before = self.read_checkpoint()
        changed = subprocess.run(
            [
                sys.executable,
                str(AGENT),
                "--job-id",
                "test-job",
                "--total-steps",
                "200",
                "--step-delay",
                "0",
                "--state-dir",
                str(self.state),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(changed.returncode, 2)
        self.assertEqual(before, self.read_checkpoint())

    def test_corrupt_checkpoint_fails_safe(self):
        path = self.state / "checkpoints" / "test-job.json"
        path.parent.mkdir(parents=True)
        path.write_text("{bad-json")
        response = self.run_agent()
        self.assertEqual(response.returncode, 2)
        self.assertEqual(path.read_text(), "{bad-json")


if __name__ == "__main__":
    unittest.main()
