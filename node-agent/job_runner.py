"""Checkpoint-capable *demo counter* workload, optionally linked to Control Plane.

Only this counter is recoverable. Arbitrary applications need their own
checkpoint/restore protocol; this does not snapshot a process or its memory.
"""

from __future__ import annotations

import os
import signal
import time

from checkpoint import CheckpointStore
from config import AgentConfig
from control_plane import ControlPlaneClient
from heartbeat import HeartbeatStore


def run_job(config: AgentConfig) -> int:
    store = CheckpointStore(config.state_dir, config.job_id, config.total_steps)
    heartbeat = HeartbeatStore(config.state_dir, config.job_id, config.total_steps)
    remote = (
        ControlPlaneClient(config.control_plane_url, config.agent_token, config.job_id)
        if config.control_plane_url and config.agent_token
        else None
    )

    existing = store.load()
    if remote:
        remote.verify_config(
            config.total_steps, config.checkpoint_every, config.heartbeat_every
        )
        from_server = remote.get_checkpoint()
        if from_server is not None:
            # A fresh replacement VM with an empty state/ directory downloads
            # its latest durable checkpoint from the shared Control Plane DB.
            server_step = from_server.get("last_completed_step", -1)
            local_step = existing["last_completed_step"] if existing else -1
            if server_step > local_step or (
                server_step == local_step and from_server.get("status") == "COMPLETED"
            ):
                store.restore_remote(from_server)
                existing = store.load()
                print(
                    f"[agent] RESTORED_FROM_CONTROL_PLANE step={server_step}",
                    flush=True,
                )
            elif local_step > server_step:
                # A previous local checkpoint might be newer than remote if
                # the process lost network connectivity during upload.
                remote.save_checkpoint(existing)
        elif existing is not None:
            remote.save_checkpoint(existing)

    if existing is not None and existing["status"] == "COMPLETED":
        print(
            f"[agent] Job {config.job_id} already COMPLETED; no work to repeat.",
            flush=True,
        )
        heartbeat.write(config.total_steps, config.total_steps, "COMPLETED")
        if remote:
            remote.send_heartbeat(config.total_steps, config.total_steps, "COMPLETED")
        return 0

    persisted = existing["last_completed_step"] if existing is not None else 0
    current = persisted
    print(
        f"[agent] job={config.job_id} RESUME_FROM={persisted} "
        f"NEXT_STEP={persisted + 1} TOTAL={config.total_steps}",
        flush=True,
    )

    def checkpoint_now(step: int, status: str) -> None:
        store.save(step, status)
        if remote:
            latest = store.load()
            assert latest is not None
            remote.save_checkpoint(latest)

    def heartbeat_now(step: int, saved: int, status: str) -> None:
        heartbeat.write(step, saved, status)
        if remote:
            remote.send_heartbeat(step, saved, status)

    if existing is None:
        checkpoint_now(0, "RUNNING")
    heartbeat_now(current, persisted, "RUNNING")

    stopping = False

    def handle_stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        print(f"[agent] Received signal={signum}; saving before exit", flush=True)
        stopping = True

    old_sigterm = signal.signal(signal.SIGTERM, handle_stop)
    old_sigint = signal.signal(signal.SIGINT, handle_stop)
    try:
        for step in range(persisted + 1, config.total_steps + 1):
            if stopping:
                break
            if config.step_delay:
                time.sleep(config.step_delay)
            current = step

            if config.simulate_crash_at == step:
                print(
                    f"[agent] SIMULATED_CRASH_AT={step}; persisted={persisted}",
                    flush=True,
                )
                os._exit(75)

            if step == config.total_steps:
                checkpoint_now(step, "COMPLETED")
                persisted = step
                heartbeat_now(step, persisted, "COMPLETED")
                print(f"[agent] COMPLETED job={config.job_id} steps={step}", flush=True)
                return 0

            if step % config.checkpoint_every == 0:
                checkpoint_now(step, "RUNNING")
                persisted = step
                print(f"[agent] CHECKPOINT step={step}", flush=True)

            if step % config.heartbeat_every == 0:
                heartbeat_now(step, persisted, "RUNNING")
                print(
                    f"[agent] HEARTBEAT step={step} persisted={persisted}", flush=True
                )

        checkpoint_now(current, "INTERRUPTED")
        heartbeat_now(current, current, "INTERRUPTED")
        print(f"[agent] INTERRUPTED saved_step={current}", flush=True)
        return 130
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)
