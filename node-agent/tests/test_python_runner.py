"""Interrupt an active child and preserve only completed iteration state."""

import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkpoint import CheckpointStore  # noqa: E402
from config import AgentConfig  # noqa: E402
from control_plane import RemoteError, RetryableRemoteError  # noqa: E402
from interruption import InterruptionNotice, StopRequest  # noqa: E402
from job_runner import run_job  # noqa: E402
from program import ProgramError, PythonProgram  # noqa: E402


class PythonRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = AgentConfig(
            job_id="script-test",
            total_steps=3,
            checkpoint_every=0,
            heartbeat_every=1,
            step_delay=0,
            state_dir=self.root,
        )

    def test_existing_torch_job_cannot_advance_without_saving_next_epoch(self):
        program = PythonProgram("pass", "forgotten.py", 3)
        with self.assertRaises(ProgramError):
            program.run(2, {"_spulti_torch": {"epoch": 1}}, StopRequest(15))

    def test_long_step_sends_periodic_heartbeat_before_step_finishes(self):
        program = PythonProgram("import time\ntime.sleep(0.2)\n", "slow.py", 1)
        config = replace(
            self.config,
            total_steps=1,
            heartbeat_every=999,
            checkpoint_seconds=3600,
            control_plane_url="http://localhost",
            agent_token="test-token",
        )
        remote = Mock()
        remote.get_checkpoint.return_value = None
        original = time.monotonic
        with (
            patch("job_runner.ControlPlaneClient", return_value=remote),
            patch(
                "job_runner.time", SimpleNamespace(monotonic=lambda: original() * 1000)
            ),
        ):
            self.assertEqual(run_job(config, program=program), 0)
        calls = [call.args for call in remote.send_heartbeat.call_args_list]
        self.assertGreaterEqual(calls.count((0, 0, "RUNNING")), 2)
        self.assertEqual(calls[-1], (1, 1, "COMPLETED"))

    def test_notice_during_long_step_stops_child_and_reports_prior_result(self):
        marker = self.root / "started"
        finished = self.root / "should-not-finish"
        program = PythonProgram(
            "import signal, time\nfrom pathlib import Path\n"
            "from spulti import state, step_number\n"
            "state['counter'] = state.get('counter', 0) + 7\n"
            "if step_number == 2:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"    Path({str(marker)!r}).touch()\n"
            "    time.sleep(10)\n"
            f"    Path({str(finished)!r}).touch()\n",
            "long.py",
            3,
        )
        watcher_done = threading.Event()
        threads = []

        def make_monitor(provider, interval, stop):
            def watch():
                while not watcher_done.wait(0.01):
                    if marker.exists():
                        stop.request(InterruptionNotice("aws", "terminate"))
                        return

            thread = threading.Thread(target=watch, daemon=True)
            threads.append(thread)
            return Mock(start=thread.start, close=watcher_done.set)

        started = time.monotonic()
        with patch("job_runner.InterruptionMonitor", side_effect=make_monitor):
            self.assertEqual(run_job(self.config, program=program), 130)
        threads[0].join(timeout=1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(finished.exists())
        store = CheckpointStore(
            self.root, "script-test", 3, source_sha256=program.source_sha256
        )
        checkpoint = store.load()
        self.assertEqual(checkpoint["last_completed_step"], 1)
        self.assertEqual(checkpoint["workload_state"], {"counter": 7})
        report = json.loads((self.root / "interruptions/script-test.json").read_text())
        self.assertEqual(report["current_step"], 1)
        self.assertEqual(report["provider"], "aws")

    def test_invalid_state_is_not_accepted_as_a_completed_step(self):
        program = PythonProgram(
            "from spulti import state\nstate['bad'] = float('nan')\n",
            "bad.py",
            3,
        )
        with self.assertRaisesRegex(ProgramError, "step 1"):
            run_job(self.config, program=program)
        checkpoint = CheckpointStore(
            self.root,
            "script-test",
            3,
            source_sha256=program.source_sha256,
        ).load()
        self.assertEqual(checkpoint["last_completed_step"], 0)
        self.assertEqual(checkpoint["workload_state"], {})

    def test_timed_checkpoints_save_completed_state_while_child_is_busy(self):
        program = PythonProgram(
            "import time\nfrom spulti import state, step_number\n"
            "state['count'] = state.get('count', 0) + 1\n"
            "if step_number == 2:\n    time.sleep(0.4)\n",
            "busy.py",
            3,
        )
        client = Mock()
        client.get_checkpoint.return_value = None
        snapshots = []
        client.save_checkpoint.side_effect = lambda data, **kwargs: snapshots.append(
            data
        )
        config = replace(
            self.config,
            checkpoint_seconds=0.1,
            control_plane_url="http://localhost",
            agent_token="token",
        )
        with patch("job_runner.ControlPlaneClient", return_value=client):
            self.assertEqual(run_job(config, program=program), 0)
        during_second = [
            item
            for item in snapshots
            if item["status"] == "RUNNING" and item["last_completed_step"] == 1
        ]
        self.assertGreaterEqual(len(during_second), 2)
        for item in snapshots:
            self.assertEqual(
                item["workload_state"].get("count", 0), item["last_completed_step"]
            )
        self.assertEqual(snapshots[-1]["status"], "COMPLETED")
        client.report_interruption.assert_not_called()

    def test_timer_retries_transient_upload_failure_during_delay(self):
        client = Mock()
        client.get_checkpoint.return_value = None
        uploads = []

        def upload(data, **kwargs):
            uploads.append(data)
            if len(uploads) == 1:
                raise RetryableRemoteError("temporary storage failure")

        client.save_checkpoint.side_effect = upload
        config = replace(
            self.config,
            total_steps=1,
            step_delay=0.4,
            checkpoint_seconds=0.1,
            control_plane_url="http://localhost",
            agent_token="token",
        )
        program = PythonProgram("print('done')", "simple.py", 1)
        with patch("job_runner.ControlPlaneClient", return_value=client):
            self.assertEqual(run_job(config, program=program), 0)
        self.assertGreaterEqual(len(uploads), 3)
        self.assertEqual(uploads[1]["last_completed_step"], 0)
        self.assertEqual(uploads[-1]["last_completed_step"], 1)
        self.assertEqual(uploads[-1]["status"], "COMPLETED")
        self.assertEqual(client.send_heartbeat.call_args.args, (1, 1, "COMPLETED"))

    def test_timer_does_not_ignore_authentication_failure(self):
        client = Mock()
        client.get_checkpoint.return_value = None
        client.save_checkpoint.side_effect = RemoteError("Control Plane HTTP 401")
        config = replace(
            self.config,
            step_delay=0.3,
            checkpoint_seconds=0.1,
            control_plane_url="http://localhost",
            agent_token="token",
        )
        program = PythonProgram(
            "raise AssertionError('must not start')", "unused.py", 3
        )
        with patch("job_runner.ControlPlaneClient", return_value=client):
            with self.assertRaisesRegex(RemoteError, "401"):
                run_job(config, program=program)
