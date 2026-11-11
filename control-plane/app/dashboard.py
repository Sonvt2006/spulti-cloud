"""Owner-scoped dashboard read models. Cloud mutations remain in machines.py."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from app.agent_api import get_job
from app.auth import CurrentUser, DbSession
from app.machines import machine_response
from app.models import (
    AgentJob,
    Instance,
    Machine,
    MachineLifecycle,
    MachineType,
    RecoveryRun,
)
from app.services.cost_and_metering import Cost, estimate_costs
from app.services.instance_manager import ManagerError, owned_machine
from app.services.launch_config import load_launch_profiles
from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, or_, select

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
Days = Annotated[int, Query(ge=1, le=90)]


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
    return {
        "locations": sorted(
            (
                {"provider": p, "region": r, "zone": z}
                for p, r, z in {(p.provider, p.region, p.zone) for p in profiles}
            ),
            key=lambda row: (row["provider"], row["region"], row["zone"]),
        ),
        "launch_configured": bool(profiles),
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
