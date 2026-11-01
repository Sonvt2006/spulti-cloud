"""Local status/heartbeat marker (remote Control Plane heartbeat is a later task)."""

from __future__ import annotations

import os
from pathlib import Path

from checkpoint import utc_now, write_json_atomic


class HeartbeatStore:
    def __init__(self, state_dir: Path, job_id: str, total_steps: int):
        self.path = state_dir / "heartbeats" / f"{job_id}.json"
        self.job_id = job_id
        self.total_steps = total_steps

    def write(self, current_step: int, persisted_step: int, status: str) -> None:
        write_json_atomic(
            self.path,
            {
                "job_id": self.job_id,
                "pid": os.getpid(),
                "status": status,
                "current_step": current_step,
                "last_checkpoint_step": persisted_step,
                "total_steps": self.total_steps,
                "timestamp": utc_now(),
            },
        )
