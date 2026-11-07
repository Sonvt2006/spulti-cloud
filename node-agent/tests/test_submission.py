"""Local program contract, credentials, and per-job locking."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interruption import StopRequest  # noqa: E402
from program import PythonProgram, validate_source  # noqa: E402
from spulti import encode_state  # noqa: E402
from submission import job_lock, owner_client, save_manifest  # noqa: E402


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_arbitrary_script_receives_state_and_normal_script_context(self):
        program = PythonProgram(
            "import os, sys\n"
            "from spulti import state, step_number, total_steps\n"
            "assert __name__ == '__main__'\n"
            "assert len(sys.argv) == 1\n"
            "assert os.environ['SPULTI_STEP'] == str(step_number)\n"
            "assert os.environ['SPULTI_TOTAL_STEPS'] == str(total_steps)\n"
            "state['product'] *= step_number\n"
            "state['nested'] = [None, True, {'result': 2.5}]\n"
            "sys.exit(0)\n",
            "product.py",
            10,
        )
        previous = {"product": 6}
        result = program.run(4, previous, StopRequest(15))
        self.assertEqual(
            result, {"product": 24, "nested": [None, True, {"result": 2.5}]}
        )
        self.assertEqual(previous, {"product": 6})

    def test_validation_does_not_execute_program(self):
        validate_source(
            "raise RuntimeError('must not execute during validation')\n"
            "def step(n, value):\n    return value + n*n\n"
        )

    def test_any_valid_python_is_accepted(self):
        for source in ("print('hello spulti')", "def step(n): return n*n"):
            validate_source(source)

    def test_invalid_programs_fail_clearly(self):
        for source in (
            "",
            "return 123",
            "def step(:",
            "x" * 16385,
        ):
            with self.subTest(source=source[:30]), self.assertRaises(ValueError):
                validate_source(source)

    def test_state_rejects_lossy_values_and_oversized_payloads(self):
        cyclic = {}
        cyclic["self"] = cyclic
        for state in (
            {1: "non-string-key"},
            {"value": (1, 2)},
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": {1, 2}},
            {"value": "x" * 8192},
            cyclic,
            [],
        ):
            with self.subTest(state_type=type(state)), self.assertRaises(ValueError):
                encode_state(state)

    def test_job_lock_blocks_second_run_and_releases(self):
        with job_lock(self.root, "test-job"):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with job_lock(self.root, "test-job"):
                    self.fail("Second job lock should not be acquired")
        with job_lock(self.root, "test-job"):
            pass

    def test_manifest_stores_job_token_privately(self):
        with job_lock(self.root, "test-job"):
            path = self.root / "jobs/test-job.json"
            save_manifest(path, {"agent_token": "private-token"})
        self.assertEqual(json.loads(path.read_text())["agent_token"], "private-token")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_reuses_cli_login_without_sending_key_to_different_url(self):
        config = self.root / "config.json"
        config.write_text(
            json.dumps({"api_url": "https://spulti.example", "api_key": "saved-key"})
        )
        with patch.dict(os.environ, {"SPULTI_CONFIG": str(config)}, clear=True):
            client = owner_client(None)
            self.assertEqual(client.token, "saved-key")
            with self.assertRaisesRegex(ValueError, "another URL"):
                owner_client("https://other.example")
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                owner_client("http://spulti.example")
