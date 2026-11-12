"""Always-on node heartbeat and Spot monitoring, including on idle VMs."""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import threading
import time
from pathlib import Path
from uuid import UUID

from checkpoint import write_json_atomic
from control_plane import NodeClient, RemoteError
from interruption import InterruptionMonitor, InterruptionNotice, StopRequest


def run_monitor(
    client: NodeClient,
    provider: str,
    state_dir: Path,
    shutdown: threading.Event,
    *,
    heartbeat_seconds: float = 20,
    poll_seconds: float = 2,
) -> int:
    spot = StopRequest(15)
    monitor = InterruptionMonitor(provider, poll_seconds, spot)
    record_path = state_dir / "nodes" / client.job_id / "interruption.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else None
    if record:
        spot.request(InterruptionNotice(**record["notice"]))

    def save_record():
        try:
            write_json_atomic(record_path, record)
        except OSError:
            print("[node] Could not persist interruption locally", flush=True)

    def report_notice():
        nonlocal record
        if spot.notice is None:
            return
        if record is None:
            record = {"notice": spot.notice.as_dict(), "reported": False}
            save_record()
        if not record["reported"]:
            try:
                client.interruption(record["notice"], deadline=time.monotonic() + 3)
                record["reported"] = True
                save_record()
                print("[node] SPOT_NOTICE acknowledged by control plane", flush=True)
            except (RemoteError, OSError, ValueError):
                print("[node] Spot report unavailable; will retry", flush=True)

    monitor.start()
    next_heartbeat = 0.0
    next_report = 0.0
    print(f"[node] Monitoring provider={provider}", flush=True)
    try:
        while not shutdown.is_set():
            now = time.monotonic()
            if now >= next_report:
                report_notice()
                next_report = time.monotonic() + 2
            if now >= next_heartbeat:
                try:
                    client.heartbeat(deadline=time.monotonic() + 3)
                    print("[node] HEARTBEAT acknowledged", flush=True)
                except (RemoteError, OSError, ValueError):
                    print("[node] Heartbeat unavailable; will retry", flush=True)
                next_heartbeat = time.monotonic() + heartbeat_seconds
            # Metadata is polled in a separate thread even during API outages.
            shutdown.wait(min(0.5, heartbeat_seconds))
        # GCP may deliver the OS shutdown signal alongside the metadata notice.
        # An ordinary service restart is not itself a Spot interruption.
        if spot.notice is None and monitor.poller is not None:
            try:
                notice = monitor.poller.poll()
                if notice:
                    spot.request(notice)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        report_notice()
    finally:
        monitor.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor this VM continuously")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/spulti-agent"))
    parser.add_argument("--heartbeat-seconds", type=float, default=20)
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args(argv)
    for value in (args.heartbeat_seconds, args.poll_seconds):
        if not math.isfinite(value) or not 0 < value <= 60:
            parser.error("Monitor intervals must be between 0 and 60 seconds")
    request_id = str(UUID(os.environ["SPULTI_NODE_REQUEST_ID"]))
    provider = os.environ["SPULTI_CLOUD_PROVIDER"].lower()
    if provider not in {"aws", "gcp"}:
        parser.error("Node monitor requires aws or gcp metadata")
    client = NodeClient(
        os.environ["SPULTI_CONTROL_PLANE_URL"],
        os.environ["SPULTI_NODE_TOKEN"],
        request_id,
    )
    shutdown = threading.Event()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, lambda *_: shutdown.set())
    try:
        return run_monitor(
            client,
            provider,
            args.state_dir,
            shutdown,
            heartbeat_seconds=args.heartbeat_seconds,
            poll_seconds=args.poll_seconds,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
