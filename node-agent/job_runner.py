"""Run Python submissions or the built-in demo with checkpoint and Spot handling."""

from __future__ import annotations

import json
import os
import signal
import time

from checkpoint import CheckpointStore, write_json_atomic
from config import AgentConfig
from control_plane import ControlPlaneClient, RemoteError, RetryableRemoteError
from heartbeat import HeartbeatStore
from interruption import InterruptionMonitor, InterruptionNotice, StopRequest
from program import ProgramError, PythonProgram, StepInterruptedError


def _details(state: int | dict, *, upper: bool = False) -> str:
    name = "state" if isinstance(state, dict) else "sum"
    return f"{name.upper() if upper else name}={json.dumps(state, ensure_ascii=True)}"


def _finish_interruption(
    config: AgentConfig,
    stop: StopRequest,
    store: CheckpointStore,
    heartbeat: HeartbeatStore,
    remote: ControlPlaneClient | None,
    current: int,
    confirmed: int,
    state: int | dict,
) -> int:
    """Always attempt reporting, even when the final checkpoint cannot be saved."""
    assert stop.notice is not None
    final_status = "COMPLETED" if current == config.total_steps else "INTERRUPTED"
    local_step = None
    checkpoint_status = "failed"
    checkpoint_error = None
    try:
        store.save(current, final_status, state)
        local_step = current
    except (OSError, ValueError):
        checkpoint_error = "local_save_failed"
    else:
        if remote:
            try:
                # Reserve time to report failure even if storage is unavailable.
                remote.save_checkpoint(store.load(), deadline=stop.deadline - 4.0)
                confirmed = current
                checkpoint_status = "uploaded"
            except (OSError, ValueError, RemoteError):
                checkpoint_error = "upload_failed"
        else:
            checkpoint_status = "local_only"

    report = {
        **stop.notice.as_dict(),
        "current_step": current,
        "last_checkpoint_step": confirmed,
        "local_checkpoint_step": local_step,
        "checkpoint_status": checkpoint_status,
        "checkpoint_error": checkpoint_error,
    }
    reported = False
    if remote:
        try:
            remote.report_interruption(report, deadline=stop.deadline)
            reported = True
        except (OSError, ValueError, RemoteError):
            print(
                "[agent] Interruption report unavailable; keeping local record",
                flush=True,
            )
    try:
        heartbeat.write(
            current, confirmed if remote else (local_step or 0), final_status
        )
        write_json_atomic(
            config.state_dir / "interruptions" / f"{config.job_id}.json",
            {**report, "reported": reported},
        )
    except OSError:
        print("[agent] Could not write local interruption record", flush=True)
    print(
        f"[agent] INTERRUPTED provider={stop.notice.provider} "
        f"action={stop.notice.action} step={current} "
        f"{_details(state)} "
        f"checkpoint={checkpoint_status} reported={reported}",
        flush=True,
    )
    # Do not restart on a VM about to disappear, including on upload failure.
    return 130


def run_job(config: AgentConfig, *, program: PythonProgram | None = None) -> int:
    store = CheckpointStore(
        config.state_dir,
        config.job_id,
        config.total_steps,
        source_sha256=program.source_sha256 if program else None,
    )
    heartbeat = HeartbeatStore(config.state_dir, config.job_id, config.total_steps)
    existing = store.load()  # Validate local state before installing handlers.
    stop = StopRequest(config.shutdown_timeout)
    remote = (
        ControlPlaneClient(
            config.control_plane_url, config.agent_token, config.job_id, stop.event
        )
        if config.control_plane_url and config.agent_token
        else None
    )
    monitor = InterruptionMonitor(
        config.cloud_provider, config.interruption_poll_interval, stop
    )
    current = existing["last_completed_step"] if existing else 0

    def load_state(checkpoint):
        if program:
            return checkpoint["workload_state"] if checkpoint else {}
        return checkpoint["workload_state"]["running_sum"] if checkpoint else 0

    state = load_state(existing)
    confirmed = 0  # Last checkpoint acknowledged by the API, not local disk.
    persisted = current
    next_checkpoint = time.monotonic() + config.checkpoint_seconds
    next_heartbeat = time.monotonic() + 20.0

    def handle_stop(signum: int, _frame: object) -> None:
        stop.request(InterruptionNotice("signal", signal.Signals(signum).name.lower()))

    def checkpoint_now(step: int, status: str, *, periodic: bool = False) -> None:
        nonlocal confirmed, persisted, next_checkpoint
        store.save(step, status, state)
        persisted = step
        if remote:
            if periodic:
                # Bound background sync time to leave room for a Spot notice.
                remote.save_checkpoint(store.load(), deadline=time.monotonic() + 3.0)
            else:
                remote.save_checkpoint(store.load())
            confirmed = step
        next_checkpoint = time.monotonic() + config.checkpoint_seconds

    def checkpoint_if_due() -> None:
        nonlocal next_checkpoint, next_heartbeat
        if not stop.event.is_set() and time.monotonic() >= next_heartbeat:
            next_heartbeat = time.monotonic() + 20.0
            try:
                heartbeat_now(current, persisted, "RUNNING")
            except (OSError, RetryableRemoteError):
                print("[agent] Heartbeat unavailable; will retry", flush=True)
        if stop.event.is_set() or time.monotonic() < next_checkpoint:
            return
        next_checkpoint = time.monotonic() + config.checkpoint_seconds
        try:
            # This runs on the agent thread: step and result cannot race with
            # completion or interruption saves. An active child only has a copy
            # of state, so its unfinished changes never enter this snapshot.
            checkpoint_now(current, "RUNNING", periodic=True)
            print(f"[agent] TIMED_CHECKPOINT step={current}", flush=True)
        except (OSError, RetryableRemoteError):
            if not stop.event.is_set():
                print(
                    "[agent] Timed checkpoint sync failed; will retry next interval",
                    flush=True,
                )

    def heartbeat_now(step: int, saved: int, status: str) -> None:
        heartbeat.write(step, saved, status)
        if remote:
            remote.send_heartbeat(step, confirmed, status)

    old_sigterm = signal.signal(signal.SIGTERM, handle_stop)
    old_sigint = signal.signal(signal.SIGINT, handle_stop)
    old_sighup = None
    if hasattr(signal, "SIGHUP"):
        old_sighup = signal.signal(signal.SIGHUP, handle_stop)
    try:
        monitor.start()
        try:
            if remote:
                remote.verify_config(
                    config.total_steps, config.checkpoint_every, config.heartbeat_every
                )
                from_server = remote.get_checkpoint()
                if from_server is not None:
                    store.validate(from_server)
                    server_step = from_server["last_completed_step"]
                    confirmed = server_step
                    local_step = existing["last_completed_step"] if existing else -1
                    if server_step > local_step or (
                        server_step == local_step
                        and from_server.get("status") == "COMPLETED"
                    ):
                        # Load the matching step/result together before disk I/O:
                        # shutdown can still save this state if restore fails.
                        current = server_step
                        state = load_state(from_server)
                        store.restore_remote(from_server)
                        existing = store.load()
                        current = existing["last_completed_step"]
                        print(
                            f"[agent] RESTORED_FROM_CONTROL_PLANE step={server_step} "
                            f"{_details(state)}",
                            flush=True,
                        )
                    elif local_step > server_step:
                        remote.save_checkpoint(existing)
                        confirmed = local_step
                elif existing is not None:
                    remote.save_checkpoint(existing)
                    confirmed = existing["last_completed_step"]

            if existing is not None and existing["status"] == "COMPLETED":
                print(
                    f"[agent] Job {config.job_id} already COMPLETED; "
                    f"no work to repeat. {_details(state)}",
                    flush=True,
                )
                heartbeat_now(config.total_steps, config.total_steps, "COMPLETED")
                return 0

            persisted = existing["last_completed_step"] if existing else 0
            next_checkpoint = time.monotonic() + config.checkpoint_seconds
            print(
                f"[agent] job={config.job_id} RESUME_FROM={persisted} "
                f"NEXT_STEP={persisted + 1} TOTAL={config.total_steps} "
                f"{_details(state, upper=True)}",
                flush=True,
            )
            if (
                existing is None
                and config.checkpoint_every > 0
                and not stop.event.is_set()
            ):
                checkpoint_now(0, "RUNNING")
            if not stop.event.is_set():
                heartbeat_now(current, persisted, "RUNNING")

            if current == config.total_steps and not stop.event.is_set():
                checkpoint_now(current, "COMPLETED")
                heartbeat_now(current, current, "COMPLETED")
                return 0

            for step in range(persisted + 1, config.total_steps + 1):
                # Wake immediately on notice; an interrupted step is not completed.
                delay_until = time.monotonic() + config.step_delay
                while not stop.event.is_set():
                    checkpoint_if_due()
                    remaining = delay_until - time.monotonic()
                    if remaining <= 0:
                        break
                    stop.event.wait(min(remaining, 0.05))
                if stop.event.is_set():
                    break
                if program:
                    try:
                        result = program.run(
                            step,
                            state,
                            stop,
                            maintenance=checkpoint_if_due,
                        )
                    except StepInterruptedError:
                        break
                else:
                    result = state + step**2
                # Commit progress and state together only after a successful exit.
                current, state = step, result
                square = "" if program else f" SQUARE={step**2}"
                print(
                    f"[agent] STEP={step}{square} {_details(state, upper=True)}",
                    flush=True,
                )
                if config.simulate_crash_at == step:
                    print(
                        f"[agent] SIMULATED_CRASH_AT={step}; persisted={persisted}",
                        flush=True,
                    )
                    os._exit(75)
                if step == config.total_steps:
                    checkpoint_now(step, "COMPLETED")
                    heartbeat_now(step, step, "COMPLETED")
                    print(
                        f"[agent] COMPLETED job={config.job_id} steps={step} "
                        f"{_details(state)}",
                        flush=True,
                    )
                    return 0
                if config.checkpoint_every > 0 and step % config.checkpoint_every == 0:
                    checkpoint_now(step, "RUNNING")
                    persisted = step
                    print(
                        f"[agent] CHECKPOINT step={step} {_details(state)}", flush=True
                    )
                else:
                    checkpoint_if_due()
                if step % config.heartbeat_every == 0:
                    heartbeat_now(step, persisted, "RUNNING")
                    print(
                        f"[agent] HEARTBEAT step={step} persisted={persisted}",
                        flush=True,
                    )
        except ProgramError:
            if not stop.event.is_set():
                # A script failure stops the loop and preserves only the last
                # successful iteration. No fabricated Spot notice is reported.
                try:
                    checkpoint_now(current, "INTERRUPTED")
                    heartbeat_now(current, current, "INTERRUPTED")
                except (OSError, ValueError, RemoteError):
                    print(
                        "[agent] Could not sync checkpoint after script failure",
                        flush=True,
                    )
                raise
        except (OSError, ValueError, RemoteError):
            if not stop.event.is_set():
                raise
            # A cancelled request during a notice still reaches cleanup.
        return _finish_interruption(
            config, stop, store, heartbeat, remote, current, confirmed, state
        )
    finally:
        monitor.close()
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)
        if old_sighup is not None:
            signal.signal(signal.SIGHUP, old_sighup)
