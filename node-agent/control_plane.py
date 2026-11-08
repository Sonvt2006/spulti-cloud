"""Tiny stdlib HTTP client for the Control Plane agent API.

No plaintext agent token over remote HTTP. Loopback HTTP is permitted for local
runs; when deploying across machines use HTTPS (e.g. via a TLS reverse proxy).
"""

from __future__ import annotations

import json
import time
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class RemoteError(RuntimeError):
    pass


class RetryableRemoteError(RemoteError):
    """Transient connectivity/storage failure; a later snapshot can be retried."""


def validate_control_plane_url(base_url: str) -> str:
    url = urlparse(base_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid Control Plane URL")
    if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError(
            "Non-loopback Control Plane URL MUST use HTTPS to protect tokens"
        )
    return base_url.rstrip("/")


class ControlPlaneClient:
    def __init__(
        self, base_url: str, token: str, job_id: str, stop_event: Event | None = None
    ):
        self.base_url = validate_control_plane_url(base_url)
        self.token = token
        self.job_id = job_id
        self.stop_event = stop_event

    def _request(
        self,
        method: str,
        suffix: str,
        payload: dict | None = None,
        *,
        allow_404: bool = False,
        deadline: float | None = None,
    ) -> dict | None:
        url = f"{self.base_url}/api/agent/jobs/{self.job_id}/{suffix}"
        encoded = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            url,
            data=encoded,
            method=method,
            headers={"X-Agent-Token": self.token, "Content-Type": "application/json"},
        )
        for attempt in range(3):
            if deadline is None and self.stop_event and self.stop_event.is_set():
                raise RemoteError("Request cancelled for graceful shutdown")
            timeout = 10.0
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RetryableRemoteError("Request budget exhausted")
                timeout = min(3.0, remaining)
            try:
                with urlopen(request, timeout=timeout) as response:
                    raw = response.read(128 * 1024 + 1)
                    if len(raw) > 128 * 1024:
                        raise RemoteError("Control Plane response too large")
                    return json.loads(raw.decode("utf-8"))
            except HTTPError as exc:
                try:
                    if exc.code == 404 and allow_404:
                        return None
                    # Do not silently ignore failed uploads or bad tokens.
                    if exc.code not in (429, 500, 502, 503, 504):
                        detail = exc.read(256).decode("utf-8", "replace")
                        raise RemoteError(
                            f"Control Plane HTTP {exc.code}: {detail}"
                        ) from exc
                    reason = f"HTTP {exc.code}"
                finally:
                    exc.close()
            except (URLError, TimeoutError, OSError) as exc:
                reason = str(exc)
            if attempt < 2:
                delay = 0.3 * (2**attempt)
                if deadline is not None:
                    time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
                elif self.stop_event is not None:
                    self.stop_event.wait(delay)
                else:
                    time.sleep(delay)
        raise RetryableRemoteError(
            f"Control Plane unreachable after retries ({reason})"
        )

    def get_config(self) -> dict:
        return self._request("GET", "config")

    def verify_config(
        self, total_steps: int, checkpoint_every: int, heartbeat_every: int
    ) -> None:
        info = self._request("GET", "config")
        if (
            info.get("total_steps"),
            info.get("checkpoint_every"),
            info.get("heartbeat_every"),
        ) != (total_steps, checkpoint_every, heartbeat_every):
            raise RemoteError(
                "Job config mismatch: use the exact steps"
                "/intervals returned by POST /api/agent/jobs"
            )
        if info.get("status") == "COMPLETED":
            # Agent still downloads final checkpoint to ensure local completion marker.
            pass

    def get_checkpoint(self) -> dict | None:
        return self._request("GET", "checkpoint", allow_404=True)

    def save_checkpoint(self, data: dict, *, deadline: float | None = None) -> None:
        self._request("PUT", "checkpoint", data, deadline=deadline)

    def report_interruption(self, data: dict, *, deadline: float) -> None:
        self._request("POST", "interruptions", data, deadline=deadline)

    def send_heartbeat(
        self, current_step: int, last_checkpoint_step: int, status: str
    ) -> None:
        self._request(
            "POST",
            "heartbeat",
            {
                "current_step": current_step,
                "last_checkpoint_step": last_checkpoint_step,
                "status": status,
            },
        )
