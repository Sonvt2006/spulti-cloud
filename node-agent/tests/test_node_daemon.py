"""Continuous monitoring survives idle time, completed jobs, and API outages."""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control_plane import NodeClient, RetryableRemoteError  # noqa: E402
from interruption import InterruptionNotice  # noqa: E402
from node_daemon import run_monitor  # noqa: E402


class NodeDaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shutdown = threading.Event()
        self.client = Mock(spec=NodeClient)
        self.client.job_id = str(uuid4())
        output = patch("builtins.print")
        output.start()
        self.addCleanup(output.stop)

    def monitor(self, notice=None):
        def make_monitor(provider, interval, stop):
            monitor = Mock()
            monitor.poller.poll.return_value = None
            if notice:
                monitor.start.side_effect = lambda: stop.request(notice)
            return monitor

        return patch("node_daemon.InterruptionMonitor", side_effect=make_monitor)

    def run_agent(self):
        return run_monitor(
            self.client, "aws", self.root, self.shutdown, heartbeat_seconds=0.01
        )

    def test_idle_node_heartbeats_repeatedly_and_normal_stop_is_not_preemption(self):
        def heartbeat(**kwargs):
            if self.client.heartbeat.call_count == 3:
                self.shutdown.set()

        self.client.heartbeat.side_effect = heartbeat
        with self.monitor():
            self.assertEqual(self.run_agent(), 0)
        self.assertEqual(self.client.heartbeat.call_count, 3)
        self.client.interruption.assert_not_called()

    def test_api_outage_does_not_stop_monitor(self):
        def heartbeat(**kwargs):
            if self.client.heartbeat.call_count == 1:
                raise RetryableRemoteError("offline")
            self.shutdown.set()

        self.client.heartbeat.side_effect = heartbeat
        with self.monitor():
            self.assertEqual(self.run_agent(), 0)
        self.assertEqual(self.client.heartbeat.call_count, 2)

    def test_spot_report_retries_same_event_and_keeps_heartbeat_running(self):
        notice = InterruptionNotice("aws", "terminate")
        self.client.interruption.side_effect = [RetryableRemoteError("offline"), None]

        def heartbeat(**kwargs):
            if self.client.interruption.call_count >= 2:
                self.shutdown.set()
            if self.client.heartbeat.call_count > 500:
                self.fail("Interruption was not retried")

        self.client.heartbeat.side_effect = heartbeat
        with self.monitor(notice):
            self.assertEqual(self.run_agent(), 0)
        reports = self.client.interruption.call_args_list
        self.assertEqual(len(reports), 2)
        self.assertEqual(reports[0].args[0], reports[1].args[0])
        self.assertEqual(reports[0].args[0]["event_id"], notice.event_id)
        record = json.loads(next(self.root.rglob("interruption.json")).read_text())
        self.assertTrue(record["reported"])
        self.assertGreater(self.client.heartbeat.call_count, 1)

    def test_pending_notice_survives_daemon_restart(self):
        notice = InterruptionNotice("gcp", "preempt")
        path = self.root / "nodes" / self.client.job_id / "interruption.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"notice": notice.as_dict(), "reported": False}))
        self.client.heartbeat.side_effect = lambda **kwargs: self.shutdown.set()
        with self.monitor():
            self.run_agent()
        self.client.interruption.assert_called_once()
        self.assertEqual(self.client.interruption.call_args.args[0], notice.as_dict())

    def test_shutdown_checks_for_metadata_notice_before_exiting(self):
        notice = InterruptionNotice("gcp", "preempt")
        monitor = Mock()
        monitor.poller.poll.return_value = notice
        self.client.heartbeat.side_effect = lambda **kwargs: self.shutdown.set()
        with patch("node_daemon.InterruptionMonitor", return_value=monitor):
            self.run_agent()
        self.assertEqual(self.client.interruption.call_args.args[0], notice.as_dict())

    def test_client_uses_node_api_not_job_endpoint(self):
        client = NodeClient("https://api.example.test", "x" * 64, str(uuid4()))
        with patch.object(client, "_request") as request:
            client.heartbeat(deadline=123)
        request.assert_called_once_with("POST", "heartbeat", {}, deadline=123)
        self.assertEqual(client.api_path, f"/api/agent/nodes/{client.job_id}")
