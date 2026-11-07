"""Durable, atomic JSON checkpoints for the Node Agent MVP."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from spulti import encode_state

SCHEMA_VERSION = 2  # Built-in demo; submitted scripts use version 3.


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
    def __init__(
        self,
        state_dir: Path,
        job_id: str,
        total_steps: int,
        *,
        source_sha256: str | None = None,
    ):
        self.state_dir = state_dir
        self.job_id = job_id
        self.total_steps = total_steps
        self.source_sha256 = source_sha256
        self.path = state_dir / "checkpoints" / f"{job_id}.json"

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Checkpoint khong doc duoc: {self.path} ({exc})") from exc

        self.validate(data)
        return data

    def validate(self, payload: dict[str, Any]) -> None:
        """Validate both progress and the workload state before using either."""
        if not isinstance(payload, dict):
            raise ValueError("Invalid checkpoint JSON")
        version = 3 if self.source_sha256 else SCHEMA_VERSION
        if payload.get("version") != version:
            raise ValueError(
                f"Checkpoint requires version {version} for this workload; "
                "use a new job-id/state-dir for incompatible checkpoints"
            )
        step = payload.get("last_completed_step")
        status = payload.get("status")
        state = payload.get("workload_state")
        if not (
            payload.get("job_id") == self.job_id
            and type(payload.get("total_steps")) is int
            and payload["total_steps"] == self.total_steps
            and type(step) is int
            and 0 <= step <= self.total_steps
            and status in {"RUNNING", "INTERRUPTED", "COMPLETED"}
            and (status != "COMPLETED" or step == self.total_steps)
            and isinstance(state, dict)
        ):
            raise ValueError("Checkpoint does not match job, step, or workload state")
        if self.source_sha256:
            if (
                payload.get("workload") != "python_script"
                or payload.get("source_sha256") != self.source_sha256
            ):
                raise ValueError(
                    "Checkpoint does not match the submitted Python script"
                )
            encode_state(state)
        elif not (
            payload.get("workload") == "sum_of_squares"
            and type(state.get("running_sum")) is int
            and state["running_sum"] == step * (step + 1) * (2 * step + 1) // 6
        ):
            raise ValueError("Checkpoint does not match sum-of-squares state")

    def restore_remote(self, payload: dict[str, Any]) -> None:
        """Validate snapshot before overwriting local checkpoint."""
        self.validate(payload)
        write_json_atomic(self.path, payload)

    def save(
        self, last_completed_step: int, status: str, running_sum: int | dict
    ) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "total_steps": self.total_steps,
            "last_completed_step": last_completed_step,
            "status": status,
            "updated_at": utc_now(),
            "workload": "sum_of_squares",
            "workload_state": {"running_sum": running_sum},
        }
        if self.source_sha256:
            payload.update(
                version=3,
                workload="python_script",
                workload_state=running_sum,
                source_sha256=self.source_sha256,
            )
        self.validate(payload)
        write_json_atomic(self.path, payload)
