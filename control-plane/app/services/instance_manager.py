"""Persist launch intent first; reconcile one cloud operation per worker delivery."""

import hashlib
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from app.adapters.base import CreateInstanceSpec, InstanceInfo
from app.adapters.cloud_init import build_aws_user_data
from app.models import (
    AgentJob,
    Instance,
    InstanceAttempt,
    Machine,
    MachineJob,
    MachineLifecycle,
    SSHKey,
    User,
)
from app.services import recovery
from app.services.cloud_errors import (
    InstanceIdentityError,
    is_capacity_error,
    is_create_rejected,
    is_not_found,
)
from app.services.launch_config import launch_keys, load_launch_profiles, make_adapter
from app.services.managed_agent import agent_settings, attempt_token, startup_script
from app.services.placement import PlacementRequest, rank_candidates
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
POLL_SECONDS = 30
MAX_FAILURES = 5


class PreferredCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_type_id: int = Field(gt=0)
    region: str = Field(min_length=1, max_length=50)
    zone: str = Field(min_length=1, max_length=50)


class MachineRequest(PlacementRequest):
    name: str = Field(min_length=1, max_length=100)
    disk_size_gb: int = Field(default=30, ge=1, le=65536)
    startup_script: str = Field(default="", max_length=8000)
    job_id: UUID | None = None
    preferred_candidate: PreferredCandidate | None = None

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Tên máy không được trống")
        return value.strip()

    @field_validator("startup_script")
    @classmethod
    def script_has_shebang(cls, value: str) -> str:
        if value and not value.startswith("#!"):
            raise ValueError("startup_script phải bắt đầu bằng shebang")
        return value.replace("\r\n", "\n")


class ManagerError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def build_instance_spec(
    db: Session,
    *,
    user_id: int,
    machine_id: int,
    name: str,
    machine_type: str,
    image_id: str,
    zone: str,
    request_id: str,
    disk_size_gb: int = 30,
    gpu_type: str | None = None,
    gpu_count: int = 0,
    startup_script: str = "",
) -> CreateInstanceSpec:
    user = db.get(User, user_id)

    if user is None or not user.is_active:
        raise ValueError("Tài khoản không tồn tại hoặc đã bị vô hiệu hóa")

    keys = tuple(
        db.scalars(
            select(SSHKey.public_key)
            .where(SSHKey.user_id == user.id)
            .order_by(SSHKey.id)
        )
    )

    if not keys:
        raise ValueError("Hãy đăng ký SSH public key trước khi tạo máy")

    return CreateInstanceSpec(
        machine_id=machine_id,
        name=name,
        machine_type=machine_type,
        image_id=image_id,
        zone=zone,
        request_id=request_id,
        ssh_public_keys=keys,
        disk_size_gb=disk_size_gb,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        startup_script=startup_script,
    )


def _prepare_attempts(
    db: Session, machine: Machine, payload: MachineRequest, *, exclusions=None
) -> None:
    try:
        profiles = load_launch_profiles()
    except ValueError as exc:
        raise ManagerError(503, "Cấu hình launch của hệ thống không hợp lệ") from exc
    if not profiles:
        raise ManagerError(503, "Hệ thống chưa cấu hình image/network để tạo máy")
    placement = PlacementRequest.model_validate(
        payload.model_dump(include=set(PlacementRequest.model_fields))
    )
    candidates = rank_candidates(
        db, placement, exclusions=exclusions, allowed_keys=launch_keys(profiles)
    )
    position = db.scalar(
        select(func.max(InstanceAttempt.position)).where(
            InstanceAttempt.machine_id == machine.id
        )
    )
    # Preference affects initial placement only. Recovery re-ranks the original
    # resource requirements and keeps cross-cloud failover available.
    if (
        payload.preferred_candidate is not None
        and exclusions is None
        and position is None
    ):
        preferred = payload.preferred_candidate
        chosen = next(
            (
                candidate
                for candidate in candidates
                if (candidate.machine_type_id, candidate.region, candidate.zone)
                == (preferred.machine_type_id, preferred.region, preferred.zone)
            ),
            None,
        )
        if chosen is None:
            raise ManagerError(409, "Đề xuất đã thay đổi; hãy tìm cấu hình lại")
        candidates.remove(chosen)
        candidates.insert(0, chosen)
    position = (position or 0) + 1
    added = 0
    for candidate in candidates:
        profile = next(
            (
                p
                for p in profiles
                if (p.provider, p.region, p.zone)
                == (candidate.provider, candidate.region, candidate.zone)
                and candidate.machine_type in p.machine_types
            ),
            None,
        )
        if profile is None:
            continue
        request_id = str(uuid4())
        try:
            spec = build_instance_spec(
                db,
                user_id=machine.user_id,
                machine_id=machine.id,
                name=f"spulti-{machine.id}-{request_id[:12]}",
                machine_type=candidate.machine_type,
                image_id=profile.image_id,
                zone=candidate.zone,
                request_id=request_id,
                disk_size_gb=payload.disk_size_gb,
                # GPUs already included in the catalog machine type. No N1 add-ons.
                startup_script=(
                    startup_script(
                        str(payload.job_id) if payload.job_id else None,
                        request_id,
                        candidate.provider,
                        payload.startup_script,
                    )
                ),
            )
            if candidate.provider == "AWS":
                build_aws_user_data(spec)
        except ValueError as exc:
            raise ManagerError(422, str(exc)) from exc
        db.add(
            InstanceAttempt(
                machine_id=machine.id,
                position=position + added,
                request_id=request_id,
                machine_type_id=candidate.machine_type_id,
                provider=candidate.provider,
                region=candidate.region,
                zone=candidate.zone,
                spec=asdict(spec),
                adapter_config=profile.adapter_config(),
                state="READY",
            )
        )
        added += 1
    if not added:
        raise ManagerError(409, "Không có ứng viên phù hợp với cấu hình launch")


def create_machine(
    db: Session, user_id: int, payload: MachineRequest, request_key: str
) -> Machine:
    # Serialize submissions per owner, including quota and idempotency checks.
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active:
        raise ManagerError(403, "Tài khoản không hoạt động")
    data = payload.model_dump(mode="json")
    existing = db.scalar(
        select(MachineLifecycle).where(
            MachineLifecycle.user_id == user_id,
            MachineLifecycle.request_key == request_key,
        )
    )
    if existing:
        previous = MachineRequest.model_validate(existing.request_payload)
        if previous.model_dump(mode="json") != data:
            raise ManagerError(409, "Idempotency-Key đã được dùng với cấu hình khác")
        return db.get_one(Machine, existing.machine_id)
    try:
        limit = int(os.getenv("SPULTI_MAX_MACHINES_PER_USER", "5"))
        if limit <= 0:
            raise ValueError
    except ValueError as exc:
        raise ManagerError(503, "Hạn mức số máy của hệ thống không hợp lệ") from exc
    count = db.scalar(
        select(func.count())
        .select_from(Machine)
        .where(Machine.user_id == user_id, Machine.status != "DELETED")
    )
    if count >= limit:
        raise ManagerError(409, f"Đã đạt hạn mức {limit} máy chưa xóa")
    if not db.scalar(select(SSHKey.id).where(SSHKey.user_id == user_id).limit(1)):
        raise ManagerError(422, "Hãy đăng ký SSH public key trước khi tạo máy")
    try:
        agent_settings(require_api=payload.job_id is not None)
    except ValueError as exc:
        raise ManagerError(503, str(exc)) from exc

    if payload.job_id:
        job = db.get(
            AgentJob,
            str(payload.job_id),
            with_for_update=True,
        )
        if job is None or job.user_id != user_id:
            raise ManagerError(404, "Không tìm thấy job")
        if job.status == "COMPLETED":
            raise ManagerError(409, "Job đã hoàn thành")
        if db.scalar(select(MachineJob).where(MachineJob.job_id == job.id)):
            raise ManagerError(409, "Job đã gắn với máy khác")
    machine = Machine(
        user_id=user_id,
        name=payload.name,
        requested_vcpu=payload.vcpu,
        requested_ram_gib=payload.ram_gib,
        requested_gpu_count=payload.gpu_count,
        requested_disk_gib=Decimal(payload.disk_size_gb),
        requested_region=payload.region,
        requested_zone=payload.zone,
        optimization_mode=payload.optimization_mode,
        status="PENDING",
    )
    db.add(machine)
    db.flush()
    _prepare_attempts(db, machine, payload)
    if payload.job_id:
        db.add(MachineJob(machine_id=machine.id, job_id=str(payload.job_id)))
    db.add(
        MachineLifecycle(
            machine_id=machine.id,
            user_id=user_id,
            request_key=request_key,
            request_payload=data,
            next_action_at=datetime.now(UTC),
        )
    )
    db.commit()
    return machine


def owned_machine(db: Session, user_id: int, machine_id: int) -> Machine:
    machine = db.scalar(
        select(Machine).where(Machine.id == machine_id, Machine.user_id == user_id)
    )
    if machine is None:
        raise ManagerError(404, "Không tìm thấy máy")
    return machine


def request_delete(db: Session, user_id: int, machine_id: int) -> Machine:
    machine = owned_machine(db, user_id, machine_id)
    life = db.get(MachineLifecycle, machine_id)
    if life is None:
        raise ManagerError(409, "Máy cũ chưa có dữ liệu lifecycle để xóa an toàn")
    # Deliberately no Machine row lock: deletion intent can arrive during create.
    life.delete_requested = True
    life.blocked = False
    life.failures = 0
    life.next_action_at = datetime.now(UTC)
    db.commit()
    return machine


def retry_machine(db: Session, user_id: int, machine_id: int) -> Machine:
    owned_machine(db, user_id, machine_id)
    machine = db.scalar(
        select(Machine)
        .where(Machine.id == machine_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    life = db.get(MachineLifecycle, machine_id, populate_existing=True)
    if life is None or machine.status == "DELETED":
        raise ManagerError(409, "Máy không thể retry")
    if not life.blocked:
        return machine
    unfinished = db.scalar(
        select(InstanceAttempt.id)
        .where(
            InstanceAttempt.machine_id == machine_id,
            InstanceAttempt.state.in_(["CREATING", "ACTIVE"]),
        )
        .limit(1)
    )
    if not life.delete_requested and unfinished is None:
        # No cloud request remains unresolved. New retry uses current placement,
        # configuration and keys, retaining previous attempts for audit/history.
        db.execute(
            update(InstanceAttempt)
            .where(
                InstanceAttempt.machine_id == machine_id,
                InstanceAttempt.state == "READY",
            )
            .values(state="REJECTED")
        )
        _prepare_attempts(
            db, machine, MachineRequest.model_validate(life.request_payload)
        )
        machine.status = "PENDING"
    life.blocked = False
    life.failures = 0
    life.last_error = None
    life.next_action_at = datetime.now(UTC)
    db.commit()
    return machine


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _record_info(db: Session, attempt: InstanceAttempt, info: InstanceInfo) -> Instance:
    if info.provider != attempt.provider or not info.instance_ref:
        raise InstanceIdentityError("Cloud trả về sai provider hoặc thiếu instance ref")
    instance = db.get(Instance, attempt.instance_id) if attempt.instance_id else None
    if (
        instance is not None
        and info.provider_instance_id != instance.provider_instance_id
    ):
        raise InstanceIdentityError("Instance đã bị thay thế ngoài control plane")
    if instance is None:
        instance = Instance(
            machine_id=attempt.machine_id,
            machine_type_id=attempt.machine_type_id,
            provider_instance_id=info.provider_instance_id,
            region=attempt.region,
            zone=attempt.zone,
            started_at=datetime.now(UTC),
        )
        db.add(instance)
        db.flush()
        attempt.instance_id = instance.id
    attempt.instance_ref = info.instance_ref
    attempt.provider_status = info.status
    attempt.private_ip = info.private_ip
    attempt.public_ip = info.public_ip
    attempt.state = "ACTIVE"
    return instance


def _end_instance(db: Session, attempt: InstanceAttempt) -> None:
    if attempt.instance_id:
        instance = db.get_one(Instance, attempt.instance_id)
        if instance.ended_at is None:
            instance.ended_at = datetime.now(UTC)
    attempt.state = "ENDED"
    attempt.private_ip = None
    attempt.public_ip = None


def _failed(life: MachineLifecycle, message: str, *, permanent: bool = False) -> None:
    life.failures += 1
    life.last_error = message
    life.blocked = permanent or life.failures >= MAX_FAILURES
    life.next_action_at = datetime.now(UTC) + timedelta(
        seconds=min(300, 5 * 2 ** min(life.failures, 6))
    )


def reconcile_machine(db: Session, machine_id: int) -> None:
    """PostgreSQL row lock serializes workers; commits precede cloud side effects.

    The create marker is committed on a separate connection while this session
    keeps the Machine lock. Thus it survives a crash without allowing a second
    worker to call cloud concurrently. Deletion intent uses a separate row.
    """
    machine = db.scalar(
        select(Machine)
        .where(Machine.id == machine_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if machine is None:
        return
    life = db.get(MachineLifecycle, machine_id, populate_existing=True)
    now = datetime.now(UTC)
    if (
        life is None
        or life.blocked
        or machine.status == "DELETED"
        or _utc(life.next_action_at) > now
    ):
        return
    run = recovery.pending_run(db, machine_id)
    if life.delete_requested:
        recovery.cancel_recovery(db, machine_id)
        run = None
    elif run and run.state in {"CLEANUP", "PLACEMENT"}:
        old = db.get(InstanceAttempt, run.failed_attempt_id)
        if old.state == "ENDED":
            recovery.plan_replacement(db, machine, life, run)
            db.commit()
            return
    attempt = db.scalar(
        select(InstanceAttempt)
        .where(
            InstanceAttempt.machine_id == machine_id,
            InstanceAttempt.state.in_(["CREATING", "ACTIVE"]),
        )
        .order_by(InstanceAttempt.position)
    )
    if attempt is None:
        if life.delete_requested:
            machine.status = "DELETED"
            life.last_error = None
        else:
            attempt = db.scalar(
                select(InstanceAttempt)
                .where(
                    InstanceAttempt.machine_id == machine_id,
                    InstanceAttempt.state == "READY",
                )
                .order_by(InstanceAttempt.position)
            )
            if attempt is None:
                if run and run.state == "STARTING":
                    run.state = "PLACEMENT"
                    recovery.plan_replacement(db, machine, life, run)
                else:
                    _failed(
                        life,
                        "Hết ứng viên có capacity; có thể thử lại sau",
                        permanent=True,
                    )
            else:
                owner = db.get(User, machine.user_id, populate_existing=True)
                if owner is None or not owner.is_active:
                    _failed(life, "Tài khoản đã bị vô hiệu hóa", permanent=True)
                else:
                    if machine.status not in {"PROVISIONING", "RECOVERING"}:
                        machine.status = "PROVISIONING"
                        db.commit()
                        return
        if attempt is None or life.delete_requested or life.blocked:
            db.commit()
            return

    # No lifecycle row lock while waiting for network IO.
    previous_failures = life.failures
    fresh_attempt = attempt.state == "READY"
    create_called = False
    try:
        adapter = make_adapter(attempt.provider, attempt.adapter_config)
        spec = CreateInstanceSpec(**attempt.spec)
        if attempt.state in {"READY", "CREATING"}:
            info = adapter.find_instance(spec) if attempt.state == "CREATING" else None
            db.refresh(life, attribute_names=["delete_requested"])
            if info is None:
                if life.delete_requested:
                    if attempt.state == "READY":
                        # No create was dispatched, so cancellation is conclusive.
                        attempt.state = "ENDED"
                        machine.status = "DELETED"
                        life.last_error = None
                        db.commit()
                        return
                    # An earlier timeout may still have an in-flight request.
                    _failed(life, "Chưa xác định kết quả tạo VM; chưa thể xác nhận xóa")
                    db.commit()
                    return
                owner = db.get(User, machine.user_id, populate_existing=True)
                if owner is None or not owner.is_active:
                    _failed(life, "Tài khoản đã bị vô hiệu hóa", permanent=True)
                    db.commit()
                    return
                if attempt.state == "READY":
                    # Never release the Machine lock between persisting the
                    # dispatch marker and calling cloud. A separate transaction
                    # makes this marker survive rollback/worker death.
                    with Session(db.get_bind().engine) as marker_db:
                        marker_db.execute(
                            update(InstanceAttempt)
                            .where(InstanceAttempt.id == attempt.id)
                            .values(state="CREATING")
                        )
                        binding = marker_db.get(MachineJob, machine_id)
                        if binding:
                            job = marker_db.get(
                                AgentJob, binding.job_id, with_for_update=True
                            )
                            job.agent_token_hash = hashlib.sha256(
                                attempt_token(attempt.request_id).encode()
                            ).hexdigest()
                            binding.active_attempt_id = attempt.id
                            binding.activated_at = datetime.now(UTC)
                        marker_db.commit()
                    db.refresh(attempt)
                create_called = True
                info = adapter.create_instance(spec)
            _record_info(db, attempt, info)
        else:
            try:
                info = adapter.get_instance(attempt.instance_ref)
            except Exception as exc:
                if not is_not_found(exc):
                    raise
                # AWS Describe may lag RunInstances; do not end a new pending VM.
                if attempt.provider == "AWS" and attempt.provider_status == "pending":
                    raise
                info = None
            if info is not None:
                _record_info(db, attempt, info)

        db.refresh(life, attribute_names=["delete_requested"])
        terminal = info is None or (
            attempt.provider == "AWS" and info.status.lower() == "terminated"
        )
        life.failures = 0
        life.last_error = None
        life.checked_at = datetime.now(UTC)
        life.next_action_at = datetime.now(UTC) + timedelta(seconds=POLL_SECONDS)
        if terminal:
            if info is None:
                attempt.provider_status = "NOT_FOUND"
            _end_instance(db, attempt)
            machine.status = "DELETED" if life.delete_requested else "RECOVERING"
            if not life.delete_requested:
                recovery.begin_recovery(db, machine, life, attempt, "INSTANCE_LOST")
        elif life.delete_requested:
            try:
                adapter.delete_instance(attempt.instance_ref)
            except Exception as exc:
                if not is_not_found(exc):
                    raise
            # Both adapters may return before absence is observable. Confirm later.
        elif run and run.state == "CLEANUP":
            adapter.delete_instance(attempt.instance_ref)
            machine.status = "RECOVERING"
        elif reason := recovery.agent_failure(db, machine_id, attempt.id):
            recovery.begin_recovery(db, machine, life, attempt, reason)
        elif info.status.upper() == "RUNNING":
            machine.status = "RUNNING"
            recovery.mark_running(db, machine, attempt)
        elif info.status.upper() in {"PENDING", "PROVISIONING", "STAGING"}:
            machine.status = "RECOVERING" if run else "PROVISIONING"
        else:
            machine.status = "RECOVERING"
            recovery.begin_recovery(db, machine, life, attempt, "INSTANCE_STOPPED")
    except SQLAlchemyError:
        # A failed DB write must roll back; the saved request remains retryable.
        raise
    except Exception as exc:
        logger.exception("Reconcile machine %s failed", machine_id)
        if create_called and is_capacity_error(exc) and attempt.state == "CREATING":
            attempt.state = "REJECTED"
            life.last_error = "Ứng viên thiếu capacity/quota; đang thử ứng viên kế tiếp"
            life.failures = 0
            life.next_action_at = datetime.now(UTC)
        elif create_called and fresh_attempt and is_create_rejected(exc):
            attempt.state = "REJECTED"
            _failed(
                life,
                "Cloud từ chối tạo VM; kiểm tra image/network/IAM rồi retry",
                permanent=True,
            )
        else:
            life.failures = previous_failures
            _failed(
                life,
                "Không thể đồng bộ VM; kiểm tra log worker và thử lại",
                permanent=isinstance(exc, InstanceIdentityError),
            )
    db.commit()
