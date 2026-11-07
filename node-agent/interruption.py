"""Spot interruption metadata and a single, shared graceful-stop request.

Only metadata is read. This module never creates, stops, or replaces a VM.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

logger = logging.getLogger(__name__)
METADATA_HOST = "http://169.254.169.254"


@dataclass(frozen=True)
class InterruptionNotice:
    provider: str
    action: str
    deadline_at: str | None = None
    detected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    event_id: str = field(default_factory=lambda: str(uuid4()))

    def as_dict(self) -> dict:
        return asdict(self)


class StopRequest:
    def __init__(self, timeout: float):
        self.event = threading.Event()
        self.notice: InterruptionNotice | None = None
        self.deadline = 0.0
        self.timeout = timeout
        self._lock = threading.RLock()

    def request(self, notice: InterruptionNotice) -> None:
        # Repeated metadata notices/signals must not restart the shutdown budget.
        with self._lock:
            if self.notice is not None:
                return
            budget = self.timeout
            if notice.deadline_at:
                remaining = (
                    datetime.fromisoformat(notice.deadline_at) - datetime.now(UTC)
                ).total_seconds()
                # An overdue/hibernate notice can arrive while the VM is still
                # alive. Leave one short attempt to report it, with no waiting.
                budget = min(budget, max(1.0, remaining - 1.0))
            self.notice = notice
            self.deadline = time.monotonic() + budget
            self.event.set()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MetadataPoller:
    def __init__(self, provider: str):
        self.provider = provider
        # Metadata tokens must never be forwarded through environment proxies.
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())
        self._token: str | None = None
        self._token_expires = 0.0

    def _read(self, path: str, *, method="GET", headers=None) -> str:
        request = Request(METADATA_HOST + path, method=method, headers=headers or {})
        with self.opener.open(request, timeout=1) as response:
            data = response.read(4097)
            if len(data) > 4096:
                raise ValueError("Metadata response too large")
            return data.decode("utf-8").strip()

    def poll(self) -> InterruptionNotice | None:
        if self.provider == "gcp":
            value = self._read(
                "/computeMetadata/v1/instance/preempted",
                headers={"Metadata-Flavor": "Google"},
            )
            if value == "TRUE":
                return InterruptionNotice("gcp", "preempt")
            if value != "FALSE":
                raise ValueError("Invalid GCP preemption metadata")
            return None

        if self.provider != "aws":
            raise ValueError("Metadata provider must be aws or gcp")
        if self._token is None or time.monotonic() >= self._token_expires:
            self._token = self._read(
                "/latest/api/token",
                method="PUT",
                headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"},
            )
            if not self._token:
                raise ValueError("Empty IMDSv2 token")
            self._token_expires = time.monotonic() + 21540
        try:
            raw = self._read(
                "/latest/meta-data/spot/instance-action",
                headers={"X-aws-ec2-metadata-token": self._token},
            )
        except HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404:
                return None  # No interruption scheduled.
            if code == 401:
                self._token = None  # Refresh on next poll; never fall back to v1.
            raise
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("action") not in {
            "terminate",
            "stop",
            "hibernate",
        }:
            raise ValueError("Invalid AWS interruption metadata")
        deadline = datetime.fromisoformat(data["time"])
        if deadline.tzinfo is None:
            raise ValueError("AWS interruption time must include timezone")
        return InterruptionNotice("aws", data["action"], deadline.isoformat())


class InterruptionMonitor:
    def __init__(self, provider: str, interval: float, stop: StopRequest):
        self.poller = MetadataPoller(provider) if provider != "none" else None
        self.interval = interval
        self.stop = stop
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.poller is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self) -> None:
        last_warning = float("-inf")
        while not self._closed.is_set() and not self.stop.event.is_set():
            try:
                notice = self.poller.poll()
                if notice:
                    self.stop.request(notice)
                    return
            except (OSError, ValueError, KeyError, TypeError):
                # A timeout, 5xx, or malformed response is not a preemption.
                # Avoid logging raw responses, URLs, or the IMDS token.
                if time.monotonic() - last_warning >= 60:
                    logger.warning("Interruption metadata unavailable; will retry")
                    last_warning = time.monotonic()
            self._closed.wait(self.interval)

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=0.1)
