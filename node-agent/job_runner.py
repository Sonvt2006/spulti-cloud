"""Demo workload with checkpoint resume and interruption-safe saving.

Only the counter demo is recoverable. Arbitrary external programs need their own
checkpoint / resume integration; this MVP does not snapshot processes or memory.
"""

from __future__ import annotations

import os
import signal
import time

from checkpoint import CheckpointStore
from config import AgentConfig
from heartbeat import HeartbeatStore


def run_job(config: AgentConfig) -> int:
    checkpoint = CheckpointStore(config.state_dir, config.job_id, config.total_steps)
    heartbeat = HeartbeatStore(config.state_dir, config.job_id, config.total_steps)
    existing = checkpoint.load()

    if existing is not None and existing["status"] == "COMPLETED":
        print(
            f"[agent] Job {config.job_id} already COMPLETED; no work to repeat.",
            flush=True,
        )
        heartbeat.write(config.total_steps, config.total_steps, "COMPLETED")
        return 0

    last_persisted_step = existing["last_completed_step"] if existing is not None else 0
    current_step = last_persisted_step
    print(
        f"[agent] job={config.job_id} RESUME_FROM={last_persisted_step} "
        f"NEXT_STEP={last_persisted_step + 1} TOTAL={config.total_steps}",
        flush=True,
    )
    if existing is None:
        checkpoint.save(0, "RUNNING")
    heartbeat.write(current_step, last_persisted_step, "RUNNING")

    stopping = False

    def handle_stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        print(f"[agent] Received signal={signum}; saving before exit", flush=True)
        stopping = True

    old_sigterm = signal.signal(signal.SIGTERM, handle_stop)
    old_sigint = signal.signal(signal.SIGINT, handle_stop)
    try:
        for step in range(last_persisted_step + 1, config.total_steps + 1):
            if stopping:
                break
            if config.step_delay:
                time.sleep(config.step_delay)
            # A real workload would perform actual work here.
            current_step = step

            # Simulate SIGKILL / unexpected power loss *before* checkpointing.
            if config.simulate_crash_at == step:
                print(
                    f"[agent] SIMULATED_CRASH_AT={step};"
                    f"persisted={last_persisted_step}",
                    flush=True,
                )
                os._exit(
                    75
                )  # Test-only; intentionally bypasses cleanup and signal handlers.

            if step == config.total_steps:
                checkpoint.save(step, "COMPLETED")
                last_persisted_step = step
                heartbeat.write(step, last_persisted_step, "COMPLETED")
                print(f"[agent] COMPLETED job={config.job_id} steps={step}", flush=True)
                return 0

            if step % config.checkpoint_every == 0:
                checkpoint.save(step, "RUNNING")
                last_persisted_step = step
                print(f"[agent] CHECKPOINT step={step}", flush=True)

            if step % config.heartbeat_every == 0:
                heartbeat.write(step, last_persisted_step, "RUNNING")
                print(
                    f"[agent] HEARTBEAT step={step};"
                    f"persisted={last_persisted_step}",
                    flush=True,
                )

        # A graceful stop can save the last completed step,
        # even between periodic checkpoints.
        checkpoint.save(current_step, "INTERRUPTED")
        heartbeat.write(current_step, current_step, "INTERRUPTED")
        print(f"[agent] INTERRUPTED saved_step={current_step}", flush=True)
        return 130
    finally:
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)
