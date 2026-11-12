from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from app.auth import CurrentUser
from app.database import get_db
from app.models import (
    Instance,
    InstanceAttempt,
    Machine,
    MachineJob,
    MachineLifecycle,
    NodeAgent,
    RecoveryRun,
)
from app.services.instance_manager import (
    MachineRequest,
    ManagerError,
    create_machine,
    owned_machine,
    request_delete,
    retry_machine,
)
from app.services.recovery import setting, utc
from app.tasks import sync_machine
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from kombu.exceptions import OperationalError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/machines", tags=["machines"])
DbSession = Annotated[Session, Depends(get_db)]


class InstanceResponse(BaseModel):
    id: int
    provider: str
    provider_instance_id: str
    instance_ref: str | None
    region: str
    zone: str
    status: str | None
    public_ip: str | None
    private_ip: str | None
    started_at: datetime
    ended_at: datetime | None


class MachineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: str
    requested_vcpu: int
    requested_ram_gib: Decimal
    requested_gpu_count: int
    requested_disk_gib: Decimal | None
    requested_region: str | None
    requested_zone: str | None
    optimization_mode: str
    created_at: datetime
    request_key: str | None = None
    delete_requested: bool = False
    needs_attention: bool = False
    last_error: str | None = None
    checked_at: datetime | None = None
    instance: InstanceResponse | None = None
    ssh_username: str = "spulti"
    job_id: str | None = None
    node_last_heartbeat_at: datetime | None = None
    node_status: str | None = None


def _instance_view(instance: Instance, attempt: InstanceAttempt) -> InstanceResponse:
    return InstanceResponse(
        id=instance.id,
        provider=attempt.provider,
        provider_instance_id=instance.provider_instance_id,
        instance_ref=attempt.instance_ref,
        region=instance.region,
        zone=instance.zone,
        status=attempt.provider_status,
        public_ip=attempt.public_ip,
        private_ip=attempt.private_ip,
        started_at=instance.started_at,
        ended_at=instance.ended_at,
    )


def machine_response(db: Session, machine: Machine) -> MachineResponse:
    result = MachineResponse.model_validate(machine)
    binding = db.get(MachineJob, machine.id)
    result.job_id = binding.job_id if binding else None
    life = db.get(MachineLifecycle, machine.id)
    if life:
        result.request_key = life.request_key
        result.delete_requested = life.delete_requested
        result.needs_attention = life.blocked
        result.last_error = life.last_error
        result.checked_at = life.checked_at
    row = db.execute(
        select(Instance, InstanceAttempt)
        .join(InstanceAttempt, InstanceAttempt.instance_id == Instance.id)
        .where(Instance.machine_id == machine.id, Instance.ended_at.is_(None))
        .order_by(Instance.id.desc())
        .limit(1)
    ).first()
    if row:
        result.instance = _instance_view(*row)
        node = db.get(NodeAgent, row[1].id)
        if node:
            result.node_last_heartbeat_at = node.last_heartbeat_at
            result.node_status = (
                "INTERRUPTING"
                if node.interruption
                else "STARTING"
                if not node.last_heartbeat_at
                else "STALE"
                if datetime.now(UTC) - utc(node.last_heartbeat_at)
                > timedelta(seconds=setting("SPULTI_HEARTBEAT_TIMEOUT_SECONDS", 90))
                else "ONLINE"
            )
    return result


def _wake_worker(machine_id: int) -> None:
    try:
        sync_machine.apply_async(args=[machine_id], retry=False)
    except OperationalError:
        # Intent is already committed. Beat will redeliver after Redis recovers.
        pass


@router.post("", status_code=202, response_model=MachineResponse)
def submit_machine(
    payload: MachineRequest,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: Annotated[UUID | None, Header()] = None,
) -> MachineResponse:
    try:
        machine = create_machine(db, user.id, payload, str(idempotency_key or uuid4()))
    except ManagerError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    result = machine_response(db, machine)
    db.commit()  # Release the owner lock for an idempotent replay too.
    _wake_worker(machine.id)
    return result


@router.get("", response_model=list[MachineResponse])
def list_machines(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MachineResponse]:
    machines = db.scalars(
        select(Machine)
        .where(Machine.user_id == user.id)
        .order_by(Machine.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [machine_response(db, machine) for machine in machines]


@router.get("/{machine_id}", response_model=MachineResponse)
def get_machine(machine_id: int, user: CurrentUser, db: DbSession) -> MachineResponse:
    try:
        return machine_response(db, owned_machine(db, user.id, machine_id))
    except ManagerError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@router.get("/{machine_id}/instances", response_model=list[InstanceResponse])
def instance_history(
    machine_id: int, user: CurrentUser, db: DbSession
) -> list[InstanceResponse]:
    try:
        owned_machine(db, user.id, machine_id)
    except ManagerError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return [
        _instance_view(instance, attempt)
        for instance, attempt in db.execute(
            select(Instance, InstanceAttempt)
            .join(InstanceAttempt, InstanceAttempt.instance_id == Instance.id)
            .where(Instance.machine_id == machine_id)
            .order_by(Instance.id)
        )
    ]


@router.delete("/{machine_id}", status_code=202, response_model=MachineResponse)
def delete_machine(
    machine_id: int, user: CurrentUser, db: DbSession
) -> MachineResponse:
    try:
        machine = request_delete(db, user.id, machine_id)
    except ManagerError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    result = machine_response(db, machine)
    _wake_worker(machine_id)
    return result


@router.post("/{machine_id}/retry", status_code=202, response_model=MachineResponse)
def retry(machine_id: int, user: CurrentUser, db: DbSession) -> MachineResponse:
    try:
        machine = retry_machine(db, user.id, machine_id)
    except ManagerError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, exc.detail) from exc
    result = machine_response(db, machine)
    db.commit()
    _wake_worker(machine_id)
    return result


@router.get("/{machine_id}/events")
def recovery_history(
    machine_id: int,
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    try:
        owned_machine(db, user.id, machine_id)
    except ManagerError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    from app.services.recovery import utc

    return [
        {
            "id": run.id,
            "reason": run.reason,
            "state": run.state,
            "failed_attempt_id": run.failed_attempt_id,
            "replacement_attempt_id": run.replacement_attempt_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "excluded_until": run.excluded_until,
            "last_error": run.last_error,
            "downtime_seconds": (
                (utc(run.finished_at) - utc(run.started_at)).total_seconds()
                if run.state == "SUCCEEDED"
                else None
            ),
        }
        for run in db.scalars(
            select(RecoveryRun)
            .where(RecoveryRun.machine_id == machine_id)
            .order_by(RecoveryRun.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ]
