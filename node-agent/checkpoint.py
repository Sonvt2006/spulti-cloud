"""Durable, atomic JSON checkpoints for the Node Agent MVP."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Never expose a partially written checkpoint/heartbeat JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temp_name = stream.name
            json.dump(payload, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


class CheckpointStore:
    def __init__(self, state_dir: Path, job_id: str, total_steps: int):
        self.state_dir = state_dir
        self.job_id = job_id
        self.total_steps = total_steps
        self.path = state_dir / "checkpoints" / f"{job_id}.json"

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Checkpoint khong doc duoc: {self.path} ({exc})") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Checkpoint khong hop le: {self.path}")
        current = data.get("last_completed_step")
        valid = (
            data.get("version") == SCHEMA_VERSION
            and data.get("job_id") == self.job_id
            and type(data.get("total_steps")) is int
            and data["total_steps"] == self.total_steps
            and type(current) is int
            and 0 <= current <= self.total_steps
            and data.get("status") in {"RUNNING", "INTERRUPTED", "COMPLETED"}
            and (data.get("status") != "COMPLETED" or current == self.total_steps)
        )
        if not valid:
            raise ValueError(
                "Checkpoint khong khop job-id/total-steps hoac bi loi. "
                f"Khong ghi de tu dong: {self.path}"
            )
        return data

    def restore_remote(self, payload: dict[str, Any]) -> None:
        """Validate snapshot before overwriting local checkpoint."""
        if not isinstance(payload, dict):
            raise ValueError("Invalid remote checkpoint JSON")
        step = payload.get("last_completed_step")
        status = payload.get("status")
        if not (
            payload.get("version") == SCHEMA_VERSION
            and payload.get("job_id") == self.job_id
            and type(payload.get("total_steps")) is int
            and payload["total_steps"] == self.total_steps
            and type(step) is int
            and 0 <= step <= self.total_steps
            and status in {"RUNNING", "INTERRUPTED", "COMPLETED"}
            and (status != "COMPLETED" or step == self.total_steps)
        ):
            raise ValueError("Remote checkpoint does not match current job")
        write_json_atomic(self.path, payload)

    def save(self, last_completed_step: int, status: str) -> None:
        if not 0 <= last_completed_step <= self.total_steps:
            raise ValueError("Invalid checkpoint step")
        if status not in {"RUNNING", "INTERRUPTED", "COMPLETED"}:
            raise ValueError("Invalid checkpoint status")
        write_json_atomic(
            self.path,
            {
                "version": SCHEMA_VERSION,
                "job_id": self.job_id,
                "total_steps": self.total_steps,
                "last_completed_step": last_completed_step,
                "status": status,
                "updated_at": utc_now(),
            },
        )
