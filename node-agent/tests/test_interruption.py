"""Deterministic interruption tests; no cloud or VM access."""

import io
import json
import signal
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from checkpoint import CheckpointStore  # noqa: E402
from config import AgentConfig, parse_args  # noqa: E402
from control_plane import ControlPlaneClient, RemoteError  # noqa: E402
from heartbeat import HeartbeatStore  # noqa: E402
from interruption import (  # noqa: E402
    InterruptionMonitor,
    InterruptionNotice,
    MetadataPoller,
    StopRequest,
)
from job_runner import run_job  # noqa: E402

UTC = timezone.utc  # noqa: UP017 - Tests also run on Ubuntu 22.04/Python 3.10.


def http_error(code):
    return HTTPError("http://169.254.169.254/", code, "test", {}, io.BytesIO())


class MetadataTests(unittest.TestCase):
    def test_aws_token_reuse_404_and_notice(self):
        poller = MetadataPoller("aws")
        deadline = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
        with patch.object(
            poller,
            "_read",
            side_effect=[
                "token-1",
                http_error(404),
                json.dumps({"action": "terminate", "time": deadline}),
            ],
        ) as read:
            self.assertIsNone(poller.poll())
            notice = poller.poll()
        self.assertEqual(notice.provider, "aws")
        self.assertEqual(notice.action, "terminate")
        self.assertEqual(notice.deadline_at, deadline)
        self.assertEqual(read.call_args_list[0].kwargs["method"], "PUT")
        self.assertEqual(
            read.call_args_list[1].kwargs["headers"],
            {"X-aws-ec2-metadata-token": "token-1"},
        )
        self.assertEqual(read.call_count, 3)

    def test_aws_401_refreshes_without_imdsv1_fallback(self):
        poller = MetadataPoller("aws")
        with patch.object(
            poller,
            "_read",
            side_effect=[
                "expired",
                http_error(401),
                "renewed",
                http_error(404),
            ],
        ) as read:
            with self.assertRaises(HTTPError):
                poller.poll()
            self.assertIsNone(poller.poll())
        self.assertEqual(read.call_args_list[2].kwargs["method"], "PUT")
        self.assertEqual(
            read.call_args_list[3].kwargs["headers"],
            {"X-aws-ec2-metadata-token": "renewed"},
        )

    def test_aws_expired_token_and_all_actions(self):
        for action in ("stop", "hibernate", "terminate"):
            with self.subTest(action=action):
                poller = MetadataPoller("aws")
                poller._token = "old"
                with patch.object(
                    poller,
                    "_read",
                    side_effect=[
                        "new",
                        json.dumps({"action": action, "time": "2026-11-07T12:00:00Z"}),
                    ],
                ) as read:
                    self.assertEqual(poller.poll().action, action)
                self.assertEqual(read.call_args_list[0].kwargs["method"], "PUT")

    def test_bad_metadata_is_not_a_notice(self):
        for raw in (
            "{}",
            "[]",
            "bad json",
            '{"action":"reboot"}',
            '{"action":"stop","time":"2026-11-07T12:00:00"}',
        ):
            with self.subTest(raw=raw):
                poller = MetadataPoller("aws")
                with patch.object(poller, "_read", side_effect=["token", raw]):
                    with self.assertRaises((ValueError, KeyError)):
                        poller.poll()

    def test_gcp_requires_true_and_header(self):
        poller = MetadataPoller("gcp")
        with patch.object(
            poller, "_read", side_effect=["FALSE", "TRUE", "unavailable"]
        ) as read:
            self.assertIsNone(poller.poll())
            notice = poller.poll()
            self.assertEqual((notice.provider, notice.action), ("gcp", "preempt"))
            self.assertIsNone(notice.deadline_at)
            with self.assertRaises(ValueError):
                poller.poll()
        self.assertEqual(
            read.call_args.kwargs["headers"], {"Metadata-Flavor": "Google"}
        )

    def test_monitor_recovers_from_errors_and_reports_once(self):
        stop = StopRequest(15)
        monitor = InterruptionMonitor("aws", 0.01, stop)
        notice = InterruptionNotice("aws", "stop")
        monitor.poller.poll = Mock(
            side_effect=[URLError("offline"), ValueError(), notice]
        )
        monitor.start()
        self.addCleanup(monitor.close)
        self.assertTrue(stop.event.wait(2))
        deadline = stop.deadline
        stop.request(InterruptionNotice("signal", "sigterm"))
        self.assertIs(stop.notice, notice)
        self.assertEqual(stop.deadline, deadline)
        self.assertEqual(monitor.poller.poll.call_count, 3)

    def test_none_never_starts_metadata_polling(self):
        with patch("interruption.MetadataPoller") as poller:
            monitor = InterruptionMonitor("none", 2, StopRequest(15))
            monitor.start()
            monitor.close()
            poller.assert_not_called()

    def test_provider_deadline_caps_shutdown_budget(self):
        stop = StopRequest(15)
        deadline = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
        stop.request(InterruptionNotice("aws", "stop", deadline))
        self.assertLessEqual(stop.deadline - time.monotonic(), 4)
        self.assertGreater(stop.deadline - time.monotonic(), 3)

    def test_overdue_notice_leaves_a_short_reporting_attempt(self):
        stop = StopRequest(15)
        past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        stop.request(InterruptionNotice("aws", "hibernate", past))
        self.assertGreater(stop.deadline - time.monotonic(), 0)
        self.assertLessEqual(stop.deadline - time.monotonic(), 1)

    def test_invalid_environment_provider_is_rejected(self):
        with patch.dict("os.environ", {"SPULTI_CLOUD_PROVIDER": "typo"}):
            with self.assertRaises(SystemExit):
                parse_args([])


class RequestBudgetTests(unittest.TestCase):
    def test_expired_budget_never_starts_request(self):
        client = ControlPlaneClient("http://localhost", "token", "job")
        with patch("control_plane.urlopen") as request:
            with self.assertRaises(RemoteError):
                client.report_interruption({}, deadline=time.monotonic() - 1)
            request.assert_not_called()

    def test_notice_cancels_regular_retry_but_allows_final_report(self):
        stop = threading.Event()
        client = ControlPlaneClient("http://localhost", "token", "job", stop)

        def fail(*args, **kwargs):
            stop.set()
            raise URLError("offline")

        with patch("control_plane.urlopen", side_effect=fail) as request:
            with self.assertRaises(RemoteError):
                client.get_checkpoint()
            self.assertEqual(request.call_count, 1)
        with patch(
            "control_plane.urlopen", return_value=io.BytesIO(b'{"status":"ack"}')
        ) as request:
            client.report_interruption({}, deadline=time.monotonic() + 1)
            self.assertLessEqual(request.call_args.kwargs["timeout"], 1)


class RunnerInterruptionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.config = AgentConfig(
            job_id="interrupt-test",
            total_steps=100,
            checkpoint_every=10,
            heartbeat_every=7,
            step_delay=0,
            state_dir=self.state,
            control_plane_url="http://localhost",
            agent_token="token",
            cloud_provider="aws",
        )
        self.client = Mock()
        self.client.get_checkpoint.return_value = None

    def run_interrupted(self, *, signal_stop=False):
        original = HeartbeatStore.write
        holder = {}

        def make_monitor(provider, interval, stop):
            holder["stop"] = stop
            return Mock()

        def heartbeat(store, current, saved, status):
            original(store, current, saved, status)
            if current == 14 and status == "RUNNING":
                if signal_stop:
                    signal.raise_signal(signal.SIGTERM)
                else:
                    holder["stop"].request(InterruptionNotice("aws", "terminate"))

        with (
            patch("job_runner.ControlPlaneClient", return_value=self.client),
            patch("job_runner.InterruptionMonitor", side_effect=make_monitor),
            patch.object(HeartbeatStore, "write", heartbeat),
        ):
            return run_job(self.config)

    def test_final_checkpoint_and_report_match_completed_work(self):
        self.assertEqual(self.run_interrupted(), 130)
        saved = CheckpointStore(self.state, "interrupt-test", 100).load()
        self.assertEqual(saved["last_completed_step"], 14)
        self.assertEqual(saved["workload_state"]["running_sum"], 1015)
        self.assertEqual(saved["status"], "INTERRUPTED")
        report = self.client.report_interruption.call_args.args[0]
        self.assertEqual(report["last_checkpoint_step"], 14)
        self.assertEqual(report["checkpoint_status"], "uploaded")
        self.client.report_interruption.assert_called_once()
        record = json.loads(
            (self.state / "interruptions/interrupt-test.json").read_text()
        )
        self.assertTrue(record["reported"])

    def test_failed_upload_still_reports_last_confirmed_checkpoint(self):
        def save(data, **kwargs):
            if data["status"] == "INTERRUPTED":
                raise RemoteError("storage unavailable")

        self.client.save_checkpoint.side_effect = save
        self.assertEqual(self.run_interrupted(), 130)
        report = self.client.report_interruption.call_args.args[0]
        self.assertEqual(report["current_step"], 14)
        self.assertEqual(report["local_checkpoint_step"], 14)
        self.assertEqual(report["last_checkpoint_step"], 10)
        self.assertEqual(report["checkpoint_error"], "upload_failed")
        heartbeat = json.loads(
            (self.state / "heartbeats/interrupt-test.json").read_text()
        )
        self.assertEqual(heartbeat["last_checkpoint_step"], 10)

    def test_disk_failure_does_not_skip_remote_report(self):
        original = CheckpointStore.save

        def save(store, step, status, running_sum):
            if status == "INTERRUPTED":
                raise OSError("disk full")
            return original(store, step, status, running_sum)

        with patch.object(CheckpointStore, "save", save):
            self.assertEqual(self.run_interrupted(), 130)
        report = self.client.report_interruption.call_args.args[0]
        self.assertEqual(report["checkpoint_error"], "local_save_failed")
        self.assertIsNone(report["local_checkpoint_step"])
        self.assertEqual(report["last_checkpoint_step"], 10)

    def test_report_failure_is_recorded_locally(self):
        self.client.report_interruption.side_effect = RemoteError("offline")
        self.assertEqual(self.run_interrupted(), 130)
        record = json.loads(
            (self.state / "interruptions/interrupt-test.json").read_text()
        )
        self.assertFalse(record["reported"])
        self.assertEqual(record["checkpoint_status"], "uploaded")

    def test_signal_uses_same_finalization_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.assertEqual(self.run_interrupted(signal_stop=True), 130)
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        report = self.client.report_interruption.call_args.args[0]
        self.assertEqual((report["provider"], report["action"]), ("signal", "sigterm"))

    def test_long_step_is_woken_without_advancing_progress(self):
        config = replace(self.config, step_delay=60)

        def make_monitor(provider, interval, stop):
            monitor = Mock()

            def start():
                timer = threading.Timer(
                    0.05, lambda: stop.request(InterruptionNotice("gcp", "preempt"))
                )
                timer.start()
                self.addCleanup(timer.join)

            monitor.start.side_effect = start
            return monitor

        started = time.monotonic()
        with (
            patch("job_runner.ControlPlaneClient", return_value=self.client),
            patch("job_runner.InterruptionMonitor", side_effect=make_monitor),
        ):
            self.assertEqual(run_job(config), 130)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(
            self.client.report_interruption.call_args.args[0]["current_step"], 0
        )
        checkpoint = CheckpointStore(self.state, "interrupt-test", 100).load()
        self.assertEqual(checkpoint["workload_state"]["running_sum"], 0)

    def test_default_delay_and_checkpoint_interval(self):
        self.assertEqual(parse_args([]).checkpoint_seconds, 10.0)
        with patch.dict("os.environ", {}, clear=True):
            config = parse_args([])
        self.assertEqual(config.step_delay, 5)
        self.assertEqual(config.checkpoint_every, 0)

    def test_stop_only_saves_latest_step_and_sum_once(self):
        self.config = replace(self.config, checkpoint_every=0)
        original = CheckpointStore.save
        saved_steps = []

        def save(store, step, status, running_sum):
            saved_steps.append((step, status, running_sum))
            return original(store, step, status, running_sum)

        with patch.object(CheckpointStore, "save", save):
            self.assertEqual(self.run_interrupted(), 130)
        self.assertEqual(saved_steps, [(14, "INTERRUPTED", 1015)])
        self.client.save_checkpoint.assert_called_once()
        self.assertEqual(
            self.client.report_interruption.call_args.args[0]["last_checkpoint_step"],
            14,
        )

    def test_completion_only_saves_final_result_once(self):
        config = replace(self.config, checkpoint_every=0, total_steps=3)
        original = CheckpointStore.save
        saved_steps = []

        def save(store, step, status, running_sum):
            saved_steps.append((step, status, running_sum))
            return original(store, step, status, running_sum)

        with (
            patch("job_runner.ControlPlaneClient", return_value=self.client),
            patch("job_runner.InterruptionMonitor"),
            patch.object(CheckpointStore, "save", save),
        ):
            self.assertEqual(run_job(config), 0)
        self.assertEqual(saved_steps, [(3, "COMPLETED", 14)])
        self.client.save_checkpoint.assert_called_once()

    def test_local_mode_records_notice_without_claiming_remote_upload(self):
        self.config = replace(self.config, control_plane_url=None, agent_token=None)
        self.assertEqual(self.run_interrupted(), 130)
        self.client.report_interruption.assert_not_called()
        record = json.loads(
            (self.state / "interruptions/interrupt-test.json").read_text()
        )
        self.assertEqual(record["checkpoint_status"], "local_only")
        self.assertEqual(record["local_checkpoint_step"], 14)
        self.assertEqual(record["last_checkpoint_step"], 0)
        self.assertFalse(record["reported"])

    def test_notice_at_completion_preserves_completed_checkpoint(self):
        config = replace(self.config, total_steps=1)

        def save(data, **kwargs):
            if data["status"] == "COMPLETED":
                signal.raise_signal(signal.SIGTERM)

        def heartbeat(current, saved, status):
            if status == "COMPLETED":
                raise RemoteError("Request cancelled for graceful shutdown")

        self.client.save_checkpoint.side_effect = save
        self.client.send_heartbeat.side_effect = heartbeat
        with (
            patch("job_runner.ControlPlaneClient", return_value=self.client),
            patch("job_runner.InterruptionMonitor"),
        ):
            self.assertEqual(run_job(config), 130)
        checkpoint = CheckpointStore(self.state, "interrupt-test", 1).load()
        self.assertEqual(checkpoint["status"], "COMPLETED")
        local_heartbeat = json.loads(
            (self.state / "heartbeats/interrupt-test.json").read_text()
        )
        self.assertEqual(local_heartbeat["status"], "COMPLETED")
        self.client.report_interruption.assert_called_once()


if __name__ == "__main__":
    unittest.main()
