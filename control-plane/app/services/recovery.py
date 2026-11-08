"""Recovery runs share the Instance Manager's Machine row lock.

Never provision a replacement until the old VM is confirmed absent. Database
intent survives worker crashes and Redis outages; every step is repeatable.
"""

import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import (
    AgentInterruption,
    AgentJob,
    InstanceAttempt,
    Machine,
    MachineJob,
    MachineLifecycle,
    MachineType,
    RecoveryRun,
)
from app.services.placement import CandidateKey
from sqlalchemy import select
from sqlalchemy.orm import Session


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def setting(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def pending_run(db: Session, machine_id: int) -> RecoveryRun | None:
    return db.scalar(
        select(RecoveryRun)
        .where(
            RecoveryRun.machine_id == machine_id,
            RecoveryRun.state.in_(["CLEANUP", "PLACEMENT", "STARTING"]),
        )
        .order_by(RecoveryRun.id.desc())
    )


def revoke_agent(db: Session, machine_id: int) -> None:
    binding = db.get(MachineJob, machine_id, populate_existing=True)
    if binding:
        job = db.get(
            AgentJob, binding.job_id, with_for_update=True, populate_existing=True
        )
        # No recoverable plaintext credential remains valid during cleanup.
        job.agent_token_hash = hashlib.sha256(uuid4().bytes).hexdigest()


def begin_recovery(
    db: Session,
    machine: Machine,
    life: MachineLifecycle,
    attempt: InstanceAttempt,
    reason: str,
) -> RecoveryRun | None:
    existing = db.scalar(
        select(RecoveryRun).where(RecoveryRun.failed_attempt_id == attempt.id)
    )
    if existing:
        return existing
    now = datetime.now(UTC)
    recent = list(
        db.scalars(
            select(RecoveryRun).where(
                RecoveryRun.machine_id == machine.id,
                RecoveryRun.started_at >= now - timedelta(hours=1),
            )
        )
    )
    machine.status = "RECOVERING"
    if len(recent) >= setting("SPULTI_RECOVERY_MAX_PER_HOUR", 5):
        life.blocked = True
        life.last_error = "Recovery rate limit reached; inspect job/cloud then retry"
        return None
    previous = pending_run(db, machine.id)
    if previous:
        previous.state = "FAILED"
        previous.finished_at = now
        previous.last_error = "Replacement failed before the job became healthy"
    run = RecoveryRun(
        machine_id=machine.id,
        failed_attempt_id=attempt.id,
        reason=reason,
        state="CLEANUP",
        started_at=now,
        excluded_until=now
        + timedelta(seconds=setting("SPULTI_RECOVERY_COOLDOWN_SECONDS", 300)),
    )
    db.add(run)
    revoke_agent(db, machine.id)
    life.failures = 0
    life.blocked = False
    life.last_error = None
    life.next_action_at = now
    db.flush()
    return run


def agent_failure(db: Session, machine_id: int, attempt_id: int) -> str | None:
    binding = db.get(MachineJob, machine_id, populate_existing=True)
    if (
        not binding
        or binding.active_attempt_id != attempt_id
        or not binding.activated_at
    ):
        return None
    job = db.get(AgentJob, binding.job_id, populate_existing=True)
    if job.status == "COMPLETED":
        return None
    notice = db.scalar(
        select(AgentInterruption)
        .where(
            AgentInterruption.job_id == job.id,
            AgentInterruption.received_at >= binding.activated_at,
        )
        .order_by(AgentInterruption.received_at.desc())
        .limit(1)
    )
    if notice:
        return (
            "SPOT_NOTICE"
            if notice.report["provider"] in {"aws", "gcp"}
            else "AGENT_STOPPED"
        )
    now = datetime.now(UTC)
    activated = utc(binding.activated_at)
    heartbeat = utc(job.last_heartbeat_at) if job.last_heartbeat_at else None
    if heartbeat and heartbeat >= activated:
        if now - heartbeat > timedelta(
            seconds=setting("SPULTI_HEARTBEAT_TIMEOUT_SECONDS", 90)
        ):
            return "HEARTBEAT_TIMEOUT"
    elif now - activated > timedelta(
        seconds=setting("SPULTI_AGENT_BOOT_GRACE_SECONDS", 600)
    ):
        return "BOOT_TIMEOUT"
    return None


def exclusions(db: Session, machine_id: int) -> dict[CandidateKey, datetime]:
    result = {}
    for run, attempt, machine_type in db.execute(
        select(RecoveryRun, InstanceAttempt, MachineType)
        .join(InstanceAttempt, InstanceAttempt.id == RecoveryRun.failed_attempt_id)
        .join(MachineType, MachineType.id == InstanceAttempt.machine_type_id)
        .where(
            RecoveryRun.machine_id == machine_id,
            RecoveryRun.excluded_until > datetime.now(UTC),
        )
    ):
        key = (attempt.provider, attempt.region, attempt.zone, machine_type.name)
        result[key] = max(
            result.get(key, utc(run.excluded_until)), utc(run.excluded_until)
        )
    return result


def plan_replacement(
    db: Session, machine: Machine, life: MachineLifecycle, run: RecoveryRun
) -> None:
    from app.services.instance_manager import (
        MachineRequest,
        ManagerError,
        _prepare_attempts,
    )

    old = db.get(InstanceAttempt, run.failed_attempt_id)
    if old.state != "ENDED":
        return
    run.state = "PLACEMENT"
    # Discard pre-failure fallbacks: re-rank current prices/risk and exclusions.
    for attempt in db.scalars(
        select(InstanceAttempt).where(
            InstanceAttempt.machine_id == machine.id, InstanceAttempt.state == "READY"
        )
    ):
        attempt.state = "REJECTED"
    try:
        with db.begin_nested():
            _prepare_attempts(
                db,
                machine,
                MachineRequest.model_validate(life.request_payload),
                exclusions=exclusions(db, machine.id),
            )
    except (ManagerError, ValueError) as exc:
        # No candidate while cooldown/quota changes: keep durable intent and retry.
        run.last_error = exc.detail if isinstance(exc, ManagerError) else str(exc)
        life.last_error = run.last_error
        life.next_action_at = datetime.now(UTC) + timedelta(seconds=30)
        return
    run.state = "STARTING"
    run.last_error = None
    life.last_error = None
    life.failures = 0
    life.next_action_at = datetime.now(UTC)


def mark_running(db: Session, machine: Machine, attempt: InstanceAttempt) -> None:
    run = pending_run(db, machine.id)
    if not run or run.state != "STARTING":
        return
    run.replacement_attempt_id = attempt.id
    binding = db.get(MachineJob, machine.id, populate_existing=True)
    if binding:
        job = db.get(AgentJob, binding.job_id, populate_existing=True)
        if (
            binding.active_attempt_id != attempt.id
            or not job.last_heartbeat_at
            or utc(job.last_heartbeat_at) < utc(binding.activated_at)
            or job.status not in {"RUNNING", "COMPLETED"}
        ):
            machine.status = "RECOVERING"
            return
    run.state = "SUCCEEDED"
    run.finished_at = datetime.now(UTC)
    run.last_error = None


def cancel_recovery(db: Session, machine_id: int) -> None:
    run = pending_run(db, machine_id)
    if run:
        run.state = "CANCELLED"
        run.finished_at = datetime.now(UTC)
    revoke_agent(db, machine_id)
