"""Read-only compute estimates from observed instance lifetimes and price history.

Never infer a missing rate from a future observation, another zone, or a discount
percentage. CostRecord is deliberately not used: its legacy snapshots have no
coverage window and can overlap this calculation. This is not provider billing.
"""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.models import Instance, SpotPrice
from sqlalchemy import select
from sqlalchemy.orm import Session

ZERO = Decimal(0)
HOUR = Decimal(3600)
PRECISION = Decimal("0.000001")


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def money(value: Decimal | None) -> str | None:
    return str(value.quantize(PRECISION)) if value is not None else None


def seconds(start: datetime, end: datetime) -> Decimal:
    return Decimal((end - start) // timedelta(microseconds=1)) / 1_000_000


def price_is_active(row: SpotPrice, at: datetime) -> bool:
    """Price intervals include recorded_at and exclude valid_until."""
    at = utc(at)
    return utc(row.recorded_at) <= at and (
        row.valid_until is None or at < utc(row.valid_until)
    )


@dataclass
class Cost:
    runtime_seconds: Decimal = ZERO
    spot: Decimal = ZERO
    on_demand: Decimal = ZERO
    missing_spot_seconds: Decimal = ZERO
    missing_on_demand_seconds: Decimal = ZERO

    def add(self, duration: Decimal, spot: SpotPrice | None, demand: SpotPrice | None):
        self.runtime_seconds += duration
        if spot is None:
            self.missing_spot_seconds += duration
        else:
            self.spot += duration * spot.price_per_hour / HOUR
        if demand is None:
            self.missing_on_demand_seconds += duration
        else:
            self.on_demand += duration * demand.price_per_hour / HOUR

    def view(self) -> dict:
        complete = not (self.missing_spot_seconds or self.missing_on_demand_seconds)
        savings = self.on_demand - self.spot if complete else None
        return {
            "currency": "USD",
            "estimated": True,
            "runtime_hours": money(self.runtime_seconds / HOUR),
            "spot_cost": money(self.spot) if not self.missing_spot_seconds else None,
            "on_demand_cost": (
                money(self.on_demand) if not self.missing_on_demand_seconds else None
            ),
            "known_spot_cost": money(self.spot),
            "known_on_demand_cost": money(self.on_demand),
            "missing_spot_hours": money(self.missing_spot_seconds / HOUR),
            "missing_on_demand_hours": money(self.missing_on_demand_seconds / HOUR),
            "savings": money(savings),
            "savings_percent": (
                money(savings / self.on_demand * 100)
                if savings is not None and self.on_demand > 0
                else None
            ),
            "complete": complete,
        }


class PriceBook:
    def __init__(self, rows: list[SpotPrice]):
        self.rows = defaultdict(list)
        self.times = defaultdict(list)
        for row in sorted(rows, key=lambda row: (utc(row.recorded_at), row.id)):
            key = (row.machine_type_id, row.region, row.zone, row.price_type)
            self.rows[key].append(row)
            self.times[key].append(utc(row.recorded_at))

    def scopes(self, instance: Instance, kind: str):
        return [
            (instance.machine_type_id, instance.region, zone, kind)
            for zone in (instance.zone, None)
        ]

    def rate(self, instance: Instance, kind: str, at: datetime) -> SpotPrice | None:
        return self.rate_for(
            instance.machine_type_id, instance.region, instance.zone, kind, at
        )

    def rate_for(
        self,
        machine_type_id: int,
        region: str,
        zone: str | None,
        kind: str,
        at: datetime,
    ) -> SpotPrice | None:
        at = utc(at)
        for scope_zone in (zone, None):
            key = (machine_type_id, region, scope_zone, kind)
            index = bisect_right(self.times[key], at) - 1
            if index >= 0:
                # Check expiry after choosing the latest version. An older,
                # open-ended version must not reappear after its replacement.
                row = self.rows[key][index]
                if price_is_active(row, at):
                    return row
        return None

    def boundaries(self, instance: Instance, start: datetime, end: datetime):
        points = {start, end}
        for kind in ("SPOT", "ON_DEMAND"):
            for key in self.scopes(instance, kind):
                points.update(at for at in self.times[key] if start < at < end)
                points.update(
                    utc(row.valid_until)
                    for row in self.rows[key]
                    if row.valid_until is not None
                    and start < utc(row.valid_until) < end
                )
        return sorted(points)


def estimate_costs(
    db: Session,
    instances: list[Instance],
    *,
    now: datetime | None = None,
    start: datetime | None = None,
    daily: bool = False,
) -> dict:
    """One price query per batch; aggregate without rounding each interval.

    A zonal rate takes precedence starting at its effective time; a regional
    rate covers earlier intervals when available. Days use UTC boundaries.
    """
    now = utc(now or datetime.now(UTC))
    start = utc(start) if start else None
    ids = {instance.machine_type_id for instance in instances}
    rows = (
        list(
            db.scalars(
                select(SpotPrice).where(
                    SpotPrice.machine_type_id.in_(ids), SpotPrice.recorded_at <= now
                )
            )
        )
        if ids
        else []
    )
    book = PriceBook(rows)
    total = Cost()
    by_machine = defaultdict(Cost)
    by_instance = {}
    by_day = defaultdict(Cost)
    if daily and start:
        day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < now:
            by_day[day.date().isoformat()]
            day += timedelta(days=1)
    active_rates = {"SPOT": ZERO, "ON_DEMAND": ZERO}
    rates_complete = {"SPOT": True, "ON_DEMAND": True}

    for instance in instances:
        cost = Cost()
        left = (
            max(utc(instance.started_at), start) if start else utc(instance.started_at)
        )
        right = min(utc(instance.ended_at), now) if instance.ended_at else now
        if right > left:
            points = book.boundaries(instance, left, right)
            for a, b in zip(points, points[1:], strict=False):
                spot = book.rate(instance, "SPOT", a)
                demand = book.rate(instance, "ON_DEMAND", a)
                duration = seconds(a, b)
                for target in (cost, total, by_machine[instance.machine_id]):
                    target.add(duration, spot, demand)
                if daily:
                    while a < b:
                        midnight = a.replace(hour=0, minute=0, second=0, microsecond=0)
                        boundary = min(b, midnight + timedelta(days=1))
                        by_day[a.date().isoformat()].add(
                            seconds(a, boundary), spot, demand
                        )
                        a = boundary
        rates = {}
        for kind, label in (("SPOT", "spot"), ("ON_DEMAND", "on_demand")):
            rate = book.rate(instance, kind, now)
            rates[f"{label}_price_per_hour"] = (
                money(rate.price_per_hour) if rate else None
            )
            rates[f"{label}_price_recorded_at"] = (
                utc(rate.recorded_at) if rate else None
            )
            rates[f"{label}_price_valid_until"] = (
                utc(rate.valid_until) if rate and rate.valid_until is not None else None
            )
            if instance.ended_at is None and utc(instance.started_at) <= now:
                if rate is None:
                    rates_complete[kind] = False
                else:
                    active_rates[kind] += rate.price_per_hour
        by_instance[instance.id] = {**cost.view(), **rates}

    return {
        "as_of": now,
        "period_start": start,
        "total": {
            **total.view(),
            "spot_price_per_hour": money(active_rates["SPOT"])
            if rates_complete["SPOT"]
            else None,
            "on_demand_price_per_hour": money(active_rates["ON_DEMAND"])
            if rates_complete["ON_DEMAND"]
            else None,
        },
        "machines": {key: cost.view() for key, cost in by_machine.items()},
        "instances": by_instance,
        "daily": [{"date": day, **cost.view()} for day, cost in sorted(by_day.items())],
    }
