"""Market Data Collector: orchestrates adapters and persists their normalized data."""

import logging
from datetime import UTC

from app.adapters.aws import AWSAdapter
from app.adapters.base import MarketData
from app.adapters.gcp import GCPAdapter
from app.database import SessionLocal
from app.models import MachineType, SpotPrice, SpotRisk
from app.services.catalog import upsert_machine_types
from sqlalchemy import func, select

logger = logging.getLogger(__name__)


def _known_types(provider: str, limit_types: int | None = None) -> dict[str, int]:
    with SessionLocal() as db:
        names = db.scalars(
            select(MachineType)
            .where(MachineType.provider == provider)
            .order_by(MachineType.name)
        ).all()
        if limit_types is not None:
            names = names[:limit_types]
        return {machine.name: machine.id for machine in names}


def save_market_data(data: MarketData, *, dry_run: bool = False) -> dict[str, int]:
    """Keep exact price-effective time; AWS risk snapshot max once per day.

    Application-level de-duplication is sufficient for a single worker MVP,
    but requires a DB constraint before scheduling concurrent collectors.
    """
    results = {
        "prices_received": len(data.prices),
        "risks_received": len(data.risks),
        "prices_saved": 0,
        "risks_saved": 0,
        "unknown_types": 0,
    }
    if dry_run:
        return results

    if not data.prices and not data.risks:
        return results

    providers = {p.provider for p in data.prices} | {r.provider for r in data.risks}
    with SessionLocal.begin() as db:
        type_ids = {
            (m.provider, m.name): m.id
            for m in db.scalars(
                select(MachineType).where(MachineType.provider.in_(providers))
            )
        }

        for item in data.prices:
            mid = type_ids.get((item.provider, item.machine_type))
            if mid is None:
                results["unknown_types"] += 1
                continue
            exists = db.scalar(
                select(SpotPrice.id)
                .where(
                    SpotPrice.machine_type_id == mid,
                    SpotPrice.region == item.region,
                    SpotPrice.zone == item.zone,
                    SpotPrice.price_type == "SPOT",
                    SpotPrice.recorded_at == item.effective_at,
                )
                .limit(1)
            )
            if exists is None:
                db.add(
                    SpotPrice(
                        machine_type_id=mid,
                        region=item.region,
                        zone=item.zone,
                        price_type="SPOT",
                        price_per_hour=item.price_per_hour,
                        recorded_at=item.effective_at,
                    )
                )
                results["prices_saved"] += 1

        for item in data.risks:
            mid = type_ids.get((item.provider, item.machine_type))
            if mid is None:
                results["unknown_types"] += 1
                continue
            # Store one snapshot per UTC calendar day per region/zone and metric.
            day = item.observed_at.astimezone(UTC).date()
            exists = db.scalar(
                select(SpotRisk.id)
                .where(
                    SpotRisk.machine_type_id == mid,
                    SpotRisk.region == item.region,
                    SpotRisk.zone == item.zone,
                    SpotRisk.source == item.source,
                    SpotRisk.metric == item.metric,
                    func.date(SpotRisk.observed_at) == day,
                )
                .limit(1)
            )
            if exists is None:
                db.add(
                    SpotRisk(
                        machine_type_id=mid,
                        region=item.region,
                        zone=item.zone,
                        source=item.source,
                        metric=item.metric,
                        rate_min=item.rate_min,
                        rate_max=item.rate_max,
                        label=item.label,
                        observed_at=item.observed_at,
                        period_start=item.period_start,
                        period_end=item.period_end,
                    )
                )
                results["risks_saved"] += 1
    return results


def collect_aws(
    regions: list[str],
    *,
    discover_types: bool = False,
    limit_types: int | None = None,
    include_risk: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    totals = {"prices_saved": 0, "risks_saved": 0, "regions_ok": 0, "regions_failed": 0}
    for region in regions:
        try:
            if discover_types:
                names = AWSAdapter.offered_machine_types(region)
                if limit_types is not None:
                    names = names[:limit_types]
                if not dry_run and names:
                    specs = AWSAdapter.describe_machine_specs(region, names)
                    with SessionLocal.begin() as db:
                        upsert_machine_types(db, specs)
            else:
                names = list(_known_types("AWS", limit_types))
            if not names:
                logger.warning("No AWS types to collect in %s", region)
                continue
            data = AWSAdapter.get_market_data(region, names, include_risk=include_risk)
            outcome = save_market_data(data, dry_run=dry_run)
            totals["prices_saved"] += outcome["prices_saved"]
            totals["risks_saved"] += outcome["risks_saved"]
            totals["regions_ok"] += 1
            logger.info(
                "AWS %s: types=%d prices=%d risks=%d",
                region,
                len(names),
                outcome["prices_received"],
                outcome["risks_received"],
            )
        except Exception:
            totals["regions_failed"] += 1
            logger.exception("Failed AWS collection for region %s", region)
    return totals


def collect_gcp(
    project_id: str,
    locations: list[tuple[str, str | None]],
    *,
    limit_types: int | None = None,
    discover_zone: str | None = None,
    include_risk: bool = True,
    dry_run: bool = False,
) -> dict[str, int]:
    totals = {
        "prices_saved": 0,
        "risks_saved": 0,
        "locations_ok": 0,
        "locations_failed": 0,
    }
    names = list(_known_types("GCP", limit_types))
    try:
        if discover_zone:
            names = GCPAdapter.offered_machine_types(project_id, discover_zone)

            if limit_types is not None:
                names = names[:limit_types]

            if names and not dry_run:
                specs = GCPAdapter.describe_machine_specs(
                    project_id, discover_zone, names
                )

                with SessionLocal.begin() as db:
                    upsert_machine_types(db, specs)
        else:
            names = list(_known_types("GCP", limit_types))

    except Exception:
        logger.exception("Failed to discover GCP machine types")
        return totals

    if not names:
        logger.warning("No GCP machine types found")
        return totals
    for region, zone in locations:
        try:
            if zone is None:
                data = GCPAdapter.get_market_data(
                    project_id,
                    region,
                    names,
                    include_risk=include_risk,
                )
            else:
                prices = GCPAdapter.get_spot_prices(
                    project_id,
                    region,
                    names,
                )

                risks = (
                    GCPAdapter.get_interruption_risks(
                        project_id,
                        region,
                        names,
                        zone=zone,
                    )
                    if include_risk
                    else []
                )

                data = MarketData(
                    prices=prices,
                    risks=risks,
                )
            outcome = save_market_data(data, dry_run=dry_run)
            totals["prices_saved"] += outcome["prices_saved"]
            totals["risks_saved"] += outcome["risks_saved"]
            totals["locations_ok"] += 1
            logger.info(
                "GCP %s: prices=%d risks=%d",
                zone or region,
                outcome["prices_received"],
                outcome["risks_received"],
            )
        except Exception:
            totals["locations_failed"] += 1
            logger.exception("Failed GCP collection for %s", zone or region)
    return totals
