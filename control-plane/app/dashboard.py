"""Owner-scoped dashboard views and managed job submissions."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from app.agent_api import get_job
from app.auth import CurrentUser, DbSession
from app.machines import _wake_worker, machine_response
from app.models import (
    AgentJob,
    AgentProgram,
    Instance,
    InstanceAttempt,
    Machine,
    MachineJob,
    MachineLifecycle,
    MachineType,
    RecoveryRun,
    SpotPrice,
    User,
)
from app.services.cost_and_metering import Cost, estimate_costs
from app.services.instance_manager import (
    MachineRequest,
    ManagerError,
    create_machine,
    owned_machine,
)
from app.services.launch_config import load_launch_profiles
from app.services.managed_agent import agent_settings
from app.services.placement import PlacementRequest
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import Field, model_validator
from sqlalchemy import func, or_, select

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
Days = Annotated[int, Query(ge=1, le=90)]
MANAGED_JOB_CONFIGURATION_ERROR = (
    "Chạy job tự động chưa sẵn sàng: cần URL HTTPS cho API và bản phát hành "
    "agent có SHA256 hợp lệ. Bạn vẫn có thể tạo máy và SSH để chạy file."
)


def period_start(now: datetime, days: int) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=days - 1
    )


def owned_instances(db, user_id: int, machine_ids=None, start=None):
    query = select(Instance).join(Machine).where(Machine.user_id == user_id)
    if machine_ids is not None:
        query = query.where(Machine.id.in_(machine_ids))
    if start is not None:
        query = query.where(or_(Instance.ended_at.is_(None), Instance.ended_at > start))
    return list(db.scalars(query.order_by(Instance.started_at, Instance.id)))


def machine_views(db, machines, instances, costs):
    types = (
        {
            item.id: item
            for item in db.scalars(
                select(MachineType).where(
                    MachineType.id.in_({i.machine_type_id for i in instances})
                )
            )
        }
        if instances
        else {}
    )
    current = {i.machine_id: i for i in instances if i.ended_at is None}
    result = []
    for machine in machines:
        item = machine_response(db, machine).model_dump(mode="json")
        item["cost"] = costs["machines"].get(machine.id, Cost().view())
        item["compute"] = None
        active = current.get(machine.id)
        if active:
            kind = types[active.machine_type_id]
            item["compute"] = {
                "machine_type": kind.name,
                "provider": kind.provider,
                "vcpu": kind.vcpu,
                "ram_gib": str(kind.ram_gib),
                "gpu_count": kind.gpu_count,
                "region": active.region,
                "zone": active.zone,
                **{
                    key: value
                    for key, value in costs["instances"][active.id].items()
                    if "price_" in key
                },
            }
        result.append(item)
    return result


# ---------------------------------------------------------------------------
# Upload & Tạo Job — one-click flow: file → job → placement → machine
# ---------------------------------------------------------------------------


class JobSubmission(PlacementRequest):
    """Dashboard form: upload a Python file and create a managed job + machine."""

    vcpu: int = Field(default=2, gt=0)
    ram_gib: Decimal = Field(default=Decimal(4), gt=0, allow_inf_nan=False)
    filename: str = Field(max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.py$")
    source_code: str = Field(min_length=1, max_length=16384)
    total_steps: int = Field(default=1, ge=1, le=10_000_000)
    checkpoint_every: int = Field(default=20, ge=0)
    step_delay: float = Field(default=0, ge=0, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_program(self):
        if len(self.source_code.encode("utf-8")) > 16384:
            raise ValueError("Program must not exceed 16 KiB")
        try:
            compile(self.source_code, self.filename, "exec")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ValueError("File Python không hợp lệ") from exc
        return self


def _submission_result(db, machine):
    attempt = db.scalars(
        select(InstanceAttempt)
        .where(InstanceAttempt.machine_id == machine.id)
        .order_by(InstanceAttempt.position)
    ).first()
    kind = db.get(MachineType, attempt.machine_type_id)
    price = db.scalar(
        select(SpotPrice.price_per_hour)
        .where(
            SpotPrice.machine_type_id == kind.id,
            SpotPrice.region == attempt.region,
            or_(SpotPrice.zone == attempt.zone, SpotPrice.zone.is_(None)),
            SpotPrice.price_type == "SPOT",
            SpotPrice.recorded_at <= attempt.created_at,
        )
        .order_by(SpotPrice.zone.is_(None), SpotPrice.recorded_at.desc())
        .limit(1)
    )
    result = machine_response(db, machine)
    return {
        "job_id": result.job_id,
        "machine": result.model_dump(mode="json"),
        "candidate": {
            "machine_type": kind.name,
            "provider": attempt.provider,
            "region": attempt.region,
            "zone": attempt.zone,
            "spot_price_per_hour": str(price) if price is not None else None,
        },
    }


@router.post("/jobs", status_code=201)
def submit_job_and_create_machine(
    payload: JobSubmission,
    user: CurrentUser,
    db: DbSession,
    idempotency_key: Annotated[UUID | None, Header()] = None,
) -> dict:
    """Persist program and launch intent together; replay lost responses safely."""
    request_key = str(idempotency_key or uuid4())
    source_hash = hashlib.sha256(payload.source_code.encode("utf-8")).hexdigest()
    placement = payload.model_dump(include=set(PlacementRequest.model_fields))
    try:
        # The same owner lock used by create_machine serializes retries and quota.
        owner = db.scalar(
            select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if owner is None or not owner.is_active:
            raise ManagerError(403, "Tài khoản không hoạt động")
        existing = db.scalar(
            select(MachineLifecycle).where(
                MachineLifecycle.user_id == user.id,
                MachineLifecycle.request_key == request_key,
            )
        )
        if existing:
            binding = db.get(MachineJob, existing.machine_id)
            job = db.get(AgentJob, binding.job_id) if binding else None
            program = db.get(AgentProgram, binding.job_id) if binding else None
            previous = MachineRequest.model_validate(existing.request_payload)
            if (
                job is None
                or program is None
                or program.filename != payload.filename
                or program.source_sha256 != source_hash
                or round(float(program.step_delay), 3) != round(payload.step_delay, 3)
                or (job.total_steps, job.checkpoint_every)
                != (payload.total_steps, payload.checkpoint_every)
                or previous.model_dump(include=set(PlacementRequest.model_fields))
                != placement
            ):
                raise ManagerError(
                    409, "Idempotency-Key đã được dùng với cấu hình khác"
                )
            machine = db.get_one(Machine, existing.machine_id)
        else:
            try:
                agent_settings()
            except ValueError as exc:
                raise ManagerError(503, MANAGED_JOB_CONFIGURATION_ERROR) from exc
            job_id = str(uuid4())
            db.add(
                AgentJob(
                    id=job_id,
                    user_id=user.id,
                    agent_token_hash=hashlib.sha256(
                        secrets.token_urlsafe(36).encode()
                    ).hexdigest(),
                    total_steps=payload.total_steps,
                    checkpoint_every=payload.checkpoint_every,
                    heartbeat_every=25,
                )
            )
            db.flush()
            db.add(
                AgentProgram(
                    job_id=job_id,
                    filename=payload.filename,
                    source_code=payload.source_code,
                    source_sha256=source_hash,
                    step_delay=round(payload.step_delay, 3),
                )
            )
            db.flush()
            machine = create_machine(
                db,
                user.id,
                MachineRequest(
                    **placement,
                    name=f"job-{payload.filename.removesuffix('.py')[:40]}-{job_id[:8]}",
                    job_id=job_id,
                ),
                request_key,
            )
        result = _submission_result(db, machine)
        db.commit()
    except ManagerError as exc:
        db.rollback()
        raise HTTPException(
            exc.status_code,
            exc.detail,
            headers={"X-Spulti-Submission-Outcome": "rejected"},
        ) from exc
    _wake_worker(machine.id)
    return result


@router.get("")
def overview(user: CurrentUser, db: DbSession, days: Days = 30) -> dict:
    now = datetime.now(UTC)
    start = period_start(now, days)
    counts = dict(
        db.execute(
            select(Machine.status, func.count())
            .where(Machine.user_id == user.id)
            .group_by(Machine.status)
        ).all()
    )
    attention = db.scalar(
        select(func.count())
        .select_from(MachineLifecycle)
        .join(Machine)
        .where(
            Machine.user_id == user.id,
            Machine.status != "DELETED",
            MachineLifecycle.blocked.is_(True),
        )
    )
    instances = owned_instances(db, user.id, start=start)
    costs = estimate_costs(db, instances, now=now, start=start, daily=True)
    recovery_query = (
        select(RecoveryRun)
        .join(Machine)
        .where(Machine.user_id == user.id, RecoveryRun.started_at >= start)
    )
    recent = list(db.scalars(recovery_query.order_by(RecoveryRun.id.desc()).limit(5)))
    recoveries = db.scalar(select(func.count()).select_from(recovery_query.subquery()))
    return {
        "as_of": now,
        "period_start": start,
        "days": days,
        "counts": {
            "total": sum(counts.values()),
            "active": sum(
                count for state, count in counts.items() if state != "DELETED"
            ),
            "running": counts.get("RUNNING", 0),
            "recovering": counts.get("RECOVERING", 0),
            "pending": counts.get("PENDING", 0) + counts.get("PROVISIONING", 0),
            "needs_attention": attention,
            "recoveries": recoveries,
        },
        "cost": costs["total"],
        "daily": costs["daily"],
        "recent_events": [
            {
                "id": run.id,
                "machine_id": run.machine_id,
                "reason": run.reason,
                "state": run.state,
                "started_at": run.started_at,
            }
            for run in recent
        ],
    }


@router.get("/options")
def options(user: CurrentUser, db: DbSession) -> dict:
    try:
        profiles = load_launch_profiles()
    except ValueError as exc:
        raise HTTPException(503, "Cấu hình launch của hệ thống không hợp lệ") from exc
    try:
        agent_settings()
        agent_configured = True
    except ValueError:
        agent_configured = False
    return {
        "locations": sorted(
            (
                {"provider": p, "region": r, "zone": z}
                for p, r, z in {(p.provider, p.region, p.zone) for p in profiles}
            ),
            key=lambda row: (row["provider"], row["region"], row["zone"]),
        ),
        "launch_configured": bool(profiles),
        "managed_agent_configured": agent_configured,
        "managed_agent_error": None
        if agent_configured
        else MANAGED_JOB_CONFIGURATION_ERROR,
    }


@router.get("/machines")
def machines(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    search: Annotated[str, Query(max_length=100)] = "",
    status: Literal["PENDING", "PROVISIONING", "RUNNING", "RECOVERING", "DELETED"]
    | None = None,
) -> dict:
    query = select(Machine).where(Machine.user_id == user.id)
    if search.strip():
        query = query.where(Machine.name.icontains(search.strip(), autoescape=True))
    if status:
        query = query.where(Machine.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = list(
        db.scalars(query.order_by(Machine.id.desc()).offset(offset).limit(limit))
    )
    instances = owned_instances(db, user.id, [m.id for m in rows]) if rows else []
    costs = estimate_costs(db, instances)
    return {
        "items": machine_views(db, rows, instances, costs),
        "total": total,
        "limit": limit,
        "offset": offset,
        "as_of": costs["as_of"],
    }


@router.get("/machines/{machine_id}")
def detail(machine_id: int, user: CurrentUser, db: DbSession, days: Days = 30) -> dict:
    try:
        machine = owned_machine(db, user.id, machine_id)
    except ManagerError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    now = datetime.now(UTC)
    instances = owned_instances(db, user.id, [machine.id])
    lifetime = estimate_costs(db, instances, now=now)
    period = estimate_costs(
        db, instances, now=now, start=period_start(now, days), daily=True
    )
    result = machine_views(db, [machine], instances, lifetime)[0]
    result.update(as_of=now, daily=period["daily"], period_cost=period["total"])
    types = (
        {
            row.id: row
            for row in db.scalars(
                select(MachineType).where(
                    MachineType.id.in_({i.machine_type_id for i in instances})
                )
            )
        }
        if instances
        else {}
    )
    result["instances"] = [
        {
            "id": i.id,
            "provider": types[i.machine_type_id].provider,
            "machine_type": types[i.machine_type_id].name,
            "provider_instance_id": i.provider_instance_id,
            "region": i.region,
            "zone": i.zone,
            "started_at": i.started_at,
            "ended_at": i.ended_at,
            "cost": lifetime["instances"][i.id],
        }
        for i in reversed(instances)
    ]
    result["job"] = None
    if result["job_id"]:
        job = db.get(AgentJob, result["job_id"])
        if job is not None and job.user_id == user.id:
            result["job"] = get_job(job.id, user, db)
            reference = job.checkpoint_data
            result["job"]["checkpoint"] = (
                {
                    "storage": "s3",
                    "bucket": reference.get("bucket"),
                    "key": reference.get("key"),
                }
                if reference and reference.get("storage") == "s3"
                else {"storage": "database"}
                if reference
                else None
            )
    return result
