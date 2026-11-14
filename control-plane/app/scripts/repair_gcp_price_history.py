"""Re-fetch exact GCP Spot scopes and repair validity without rewriting prices.

Run add_price_validity first. By default this previews API data without writes;
--apply updates matching intervals and imports returned history. Unverified
legacy intervals remain quarantined, with their original price and ID intact.
"""

import argparse
import json
import logging
import os
from collections import defaultdict

from app.adapters.base import MarketData
from app.adapters.gcp import GCPAdapter
from app.database import SessionLocal
from app.models import MachineType, SpotPrice
from app.services.market_data_collector import save_market_data
from sqlalchemy import func, select

logger = logging.getLogger(__name__)


def repair_gcp_price_history(
    project_id: str,
    *,
    regions: list[str] | None = None,
    machine_types: list[str] | None = None,
    apply: bool = False,
) -> dict:
    if not project_id.strip():
        raise ValueError("GCP project must be specified")
    filters = [MachineType.provider == "GCP", SpotPrice.price_type == "SPOT"]
    if regions:
        filters.append(SpotPrice.region.in_(regions))
    if machine_types:
        filters.append(MachineType.name.in_(machine_types))
    with SessionLocal() as db:
        scopes = list(
            db.execute(
                select(MachineType.name, SpotPrice.region, SpotPrice.zone)
                .join(SpotPrice, SpotPrice.machine_type_id == MachineType.id)
                .where(*filters)
                .distinct()
            )
        )
    totals = {
        "dry_run": not apply,
        "scopes_requested": len(scopes),
        "scopes_with_data": 0,
        "scopes_without_data": 0,
        "scopes_failed": 0,
        "scope_mismatches": 0,
        "prices_received": 0,
        "prices_saved": 0,
        "prices_updated": 0,
        "price_conflicts": 0,
    }
    # Group exact scopes to reuse the adapter's per-machine API orchestration.
    locations = defaultdict(list)
    for name, region, zone in scopes:
        locations[(region, zone)].append(name)
    for (region, zone), names in locations.items():
        try:
            data = GCPAdapter.get_market_data(
                project_id,
                region,
                names,
                zone=zone,
                include_risk=False,
            )
            prices = [
                p
                for p in data.prices
                if (
                    p.provider == "GCP"
                    and p.price_type == "SPOT"
                    and p.machine_type in names
                    and p.region == region
                    and p.zone == zone
                )
            ]
            totals["scope_mismatches"] += len(data.prices) - len(prices)
            found = {p.machine_type for p in prices}
            totals["scopes_with_data"] += len(found)
            totals["scopes_without_data"] += len(set(names) - found)
            result = save_market_data(
                MarketData(prices=prices, risks=[]), dry_run=not apply
            )
            for key in (
                "prices_received",
                "prices_saved",
                "prices_updated",
                "price_conflicts",
            ):
                totals[key] += result[key]
        except Exception:
            totals["scopes_failed"] += len(names)
            logger.exception("Failed GCP price history repair for %s", zone or region)
    with SessionLocal() as db:
        totals["quarantined_remaining"] = db.scalar(
            select(func.count())
            .select_from(SpotPrice)
            .join(MachineType, MachineType.id == SpotPrice.machine_type_id)
            .where(*filters, SpotPrice.valid_until == SpotPrice.recorded_at)
        )
    issues = any(
        totals[k]
        for k in (
            "scopes_without_data",
            "scopes_failed",
            "scope_mismatches",
            "price_conflicts",
            "quarantined_remaining",
        )
    )
    totals["status"] = "empty" if not scopes else "partial" if issues else "ok"
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.getenv("MARKET_GCP_PROJECT_ID", ""))
    parser.add_argument("--region", action="append")
    parser.add_argument("--machine-type", action="append")
    parser.add_argument("--apply", action="store_true", help="Write verified intervals")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = repair_gcp_price_history(
        args.project,
        regions=args.region,
        machine_types=args.machine_type,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2))
    if result["scopes_failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
