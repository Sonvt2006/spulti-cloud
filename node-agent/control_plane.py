"""Tiny stdlib HTTP client for the Control Plane agent API.

No plaintext agent token over remote HTTP. Loopback HTTP is permitted for local
runs; when deploying across machines use HTTPS (e.g. via a TLS reverse proxy).
"""

from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class RemoteError(RuntimeError):
    pass


class ControlPlaneClient:
    def __init__(self, base_url: str, token: str, job_id: str):
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
        if url.scheme == "http" and url.hostname not in (
            "localhost",
            "127.0.0.1",
            "::1",
        ):
            raise ValueError(
                "Non-loopback Control Plane URL MUST use "
                "HTTPS to protect the agent token"
            )
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.job_id = job_id

    def _request(
        self,
        method: str,
        suffix: str,
        payload: dict | None = None,
        *,
        allow_404: bool = False,
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
            try:
                with urlopen(request, timeout=10) as response:
                    raw = response.read(32 * 1024)
                    return json.loads(raw.decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 404 and allow_404:
                    return None
                # Non-retryable auth/schema/conflict error: fail rather than
                # pretending a checkpoint was stored successfully.
                if exc.code not in (429, 500, 502, 503, 504):
                    detail = exc.read(256).decode("utf-8", "replace")
                    raise RemoteError(
                        f"Control Plane HTTP {exc.code}: {detail}"
                    ) from exc
                reason = f"HTTP {exc.code}"
            except (URLError, TimeoutError, OSError) as exc:
                reason = str(exc)
            if attempt < 2:
                time.sleep(0.3 * (2**attempt))
        raise RemoteError(f"Control Plane unreachable after retries ({reason})")

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
                "Job config mismatch: use the exact steps/intervals "
                "returned by POST /api/agent/jobs"
            )
        if info.get("status") == "COMPLETED":
            # Agent still downloads final checkpoint to ensure local completion marker.
            pass

    def get_checkpoint(self) -> dict | None:
        return self._request("GET", "checkpoint", allow_404=True)

    def save_checkpoint(self, data: dict) -> None:
        self._request("PUT", "checkpoint", data)

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
