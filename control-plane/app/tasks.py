import logging
import os
import time
from datetime import UTC, datetime
from uuid import uuid4

from app.celery_app import celery_app
from app.database import SessionLocal
from app.models import Machine, MachineLifecycle
from app.services.instance_manager import completed_job_machines, reconcile_machine
from redis import Redis
from sqlalchemy import and_, or_, select


@celery_app.task(name="spulti.demo_add")
def demo_add(a: int, b: int) -> dict:
    # Giả lập thao tác chậm để quan sát xử lý bất đồng bộ.
    time.sleep(5)

    return {
        "operation": "demo_add",
        "result": a + b,
        "executed_by": "celery-worker",
    }


logger = logging.getLogger(__name__)

# A lease limits duplicate runs even if the previous job has not finished.
# The hard task limit (3 hours) must remain shorter than this lease (4 hours).
_UNLOCK_IF_OWNER = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _market_limit() -> int | None:
    value = os.getenv("MARKET_LIMIT_TYPES", "20").strip()
    if not value:
        return None  # Empty means all catalog machine types: may be expensive for GCP.
    limit = int(value)
    if limit <= 0:
        raise ValueError("MARKET_LIMIT_TYPES must be positive or empty")
    return limit


def _csv_env(name: str, default: str) -> list[str]:
    return [
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    ]


@celery_app.task(name="spulti.market.collect", time_limit=10800)
def collect_market_data(provider: str, dataset: str) -> dict:
    """Collect Spot prices, risks or On-demand prices, with an overlap guard."""
    provider = provider.upper()
    dataset = dataset.lower()
    if provider not in {"AWS", "GCP"} or dataset not in {
        "prices",
        "risks",
        "on_demand",
    }:
        raise ValueError(f"Invalid market collection request: {provider}/{dataset}")
    from app.services.market_data_collector import collect_aws, collect_gcp

    lock_key = f"spulti:market:lock:{provider.lower()}:{dataset}"
    token = uuid4().hex
    redis = Redis.from_url(os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
    try:
        if not redis.set(lock_key, token, nx=True, ex=14400):
            logger.warning(
                "Market collection already running: %s/%s", provider, dataset
            )
            return {"status": "skipped", "provider": provider, "dataset": dataset}
        try:
            limit = _market_limit()
            if dataset == "on_demand" and provider == "AWS":
                from app.services.on_demand_pricing import collect_aws_on_demand

                regions = _csv_env("MARKET_AWS_REGIONS", "us-east-1")
                if not regions:
                    raise ValueError("MARKET_AWS_REGIONS cannot be empty")
                return collect_aws_on_demand(regions, limit)
            if provider == "AWS":
                regions = _csv_env("MARKET_AWS_REGIONS", "us-east-1")
                if not regions:
                    raise ValueError("MARKET_AWS_REGIONS cannot be empty")
                result = collect_aws(
                    regions,
                    limit_types=limit,
                    include_price=(dataset == "prices"),
                    include_risk=(dataset == "risks"),
                )
                failed = result["regions_failed"]
                processed = result["regions_ok"]
            else:
                project = os.getenv("MARKET_GCP_PROJECT_ID", "spulti-cloud").strip()

                zones = _csv_env("MARKET_GCP_ZONES", "asia-southeast1-b")

                if not project or not zones:
                    raise ValueError("GCP project and zones must be specified")

                region_zones = [(zone.rsplit("-", 1)[0], zone) for zone in zones]

                if dataset == "on_demand":
                    from app.services.on_demand_pricing import (
                        collect_gcp_on_demand,
                    )

                    regions = sorted({region for region, _ in region_zones})
                    return collect_gcp_on_demand(regions, limit)

                if dataset == "prices":
                    locations = sorted({(region, None) for region, _ in region_zones})
                else:
                    locations = sorted(set(region_zones))

                result = collect_gcp(
                    project,
                    locations,
                    limit_types=limit,
                    include_price=(dataset == "prices"),
                    include_risk=(dataset == "risks"),
                )
                failed = result["locations_failed"]
                processed = result["locations_ok"]

            if failed:
                raise RuntimeError(
                    f"Market collection {provider}/{dataset} "
                    f"had {failed} failed locations: {result}"
                )
            if not processed:
                logger.warning(
                    "Market collection found no work: %s/%s", provider, dataset
                )
                return {
                    "status": "empty",
                    "provider": provider,
                    "dataset": dataset,
                    **result,
                }
            logger.info(
                "Market collection finished: %s/%s: %s", provider, dataset, result
            )
            return {"status": "ok", "provider": provider, "dataset": dataset, **result}
        finally:
            # Only the owner may release the lock;
            # expired leases cannot delete new locks.
            redis.eval(_UNLOCK_IF_OWNER, 1, lock_key, token)
    finally:
        redis.close()


@celery_app.task(name="spulti.sync_machine", acks_late=True, reject_on_worker_lost=True)
def sync_machine(machine_id: int) -> None:
    with SessionLocal() as db:
        reconcile_machine(db, machine_id)


@celery_app.task(name="spulti.reconcile_machines", ignore_result=True)
def reconcile_machines() -> None:
    # Database is the durable queue, so a failed Redis publish cannot lose intent.
    with SessionLocal() as db:
        ids = db.scalars(
            select(MachineLifecycle.machine_id)
            .join(Machine, Machine.id == MachineLifecycle.machine_id)
            .where(
                Machine.status != "DELETED",
                or_(
                    and_(
                        MachineLifecycle.blocked.is_(False),
                        MachineLifecycle.next_action_at <= datetime.now(UTC),
                    ),
                    and_(
                        MachineLifecycle.delete_requested.is_(False),
                        MachineLifecycle.machine_id.in_(completed_job_machines()),
                    ),
                ),
            )
            .order_by(MachineLifecycle.next_action_at, MachineLifecycle.machine_id)
            .limit(500)
        ).all()
    for machine_id in ids:
        sync_machine.apply_async(args=[machine_id], retry=False, expires=60)
