"""Xếp hạng ứng viên từ catalog; không tạo VM hoặc giữ chỗ tài nguyên."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from app.models import MachineType, MachineTypeOffering, SpotPrice, SpotRisk
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

# Nhóm địa lý được hỗ trợ tường minh; region cụ thể vẫn dùng trực tiếp được.
REGION_GROUPS = {
    "singapore": {"AWS": "ap-southeast-1", "GCP": "asia-southeast1"},
}
WEIGHTS = {"SAVING": Decimal(1), "STABLE": Decimal(0)}
CandidateKey = tuple[str, str, str, str]  # provider, region, zone, machine type


class RegionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["AWS", "GCP"]
    region: str = Field(min_length=1, max_length=50)


class ZoneSelection(RegionSelection):
    zone: str = Field(min_length=1, max_length=50)


class PlacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vcpu: int = Field(gt=0)
    ram_gib: Decimal = Field(gt=0, allow_inf_nan=False)
    gpu_count: int = Field(default=0, ge=0)
    # Catalog chưa có GPU model; từ chối thay vì chọn sai loại GPU.
    gpu_type: None = None
    provider: Literal["AWS", "GCP"] | None = None
    region: str | None = Field(default=None, min_length=1, max_length=50)
    zone: str | None = Field(default=None, min_length=1, max_length=50)
    providers: list[Literal["AWS", "GCP"]] = Field(default_factory=list)
    regions: list[RegionSelection] = Field(default_factory=list)
    zones: list[ZoneSelection] = Field(default_factory=list)
    optimization_mode: Literal["SAVING", "STABLE"] = "SAVING"
    limit: int = Field(default=20, ge=1, le=100)


class PlacementCandidate(BaseModel):
    machine_type_id: int
    provider: str
    machine_type: str
    region: str
    zone: str
    vcpu: int
    ram_gib: Decimal
    gpu_count: int
    spot_price_per_hour: Decimal
    price_recorded_at: datetime
    on_demand_price_per_hour: Decimal | None
    savings_percent: Decimal | None
    risk: Decimal
    risk_source: str
    risk_metric: str
    risk_observed_at: datetime | None
    normalized_price: Decimal = Decimal(0)
    normalized_risk: Decimal = Decimal(0)
    score: Decimal = Decimal(0)
    quota_checked: bool = False

    @property
    def key(self) -> CandidateKey:
        return (self.provider, self.region, self.zone, self.machine_type)


def _utc(value: datetime) -> datetime:
    # SQLite bỏ timezone; PostgreSQL giữ timezone.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def rank_candidates(
    db: Session,
    request: PlacementRequest,
    *,
    now: datetime | None = None,
    exclusions: Mapping[CandidateKey, datetime] | None = None,
    quota_available: Callable[[PlacementCandidate], bool] | None = None,
    default_risk: Decimal = Decimal("0.5"),
    allowed_keys: set[CandidateKey] | None = None,
) -> list[PlacementCandidate]:
    """Điểm nhỏ hơn tốt hơn; giữ thứ tự để worker thử ứng viên tiếp theo.

    Exclusions do recovery/worker cung cấp, giá trị là thời điểm hết loại trừ.
    Quota callback do caller cung cấp; thiếu callback nghĩa là chưa xác minh quota.
    Risk là chỉ số xếp hạng, không phải xác suất có cùng kỳ đo giữa AWS/GCP.
    """
    if not default_risk.is_finite() or not Decimal(0) <= default_risk <= Decimal(1):
        raise ValueError("Risk policy must be finite and between 0 and 1")
    now = _utc(now or datetime.now(UTC))
    query = (
        select(MachineType, MachineTypeOffering)
        .join(
            MachineTypeOffering, MachineTypeOffering.machine_type_id == MachineType.id
        )
        .where(
            MachineType.vcpu >= request.vcpu,
            MachineType.ram_gib >= request.ram_gib,
            MachineType.gpu_count >= request.gpu_count,
        )
    )
    if request.provider:
        query = query.where(MachineType.provider == request.provider)
    if request.zone:
        query = query.where(MachineTypeOffering.zone == request.zone)
    if request.region and request.region not in REGION_GROUPS:
        query = query.where(MachineTypeOffering.region == request.region)
    if request.providers:
        query = query.where(MachineType.provider.in_(request.providers))
    if request.regions:
        query = query.where(
            or_(
                *(
                    and_(
                        MachineType.provider == item.provider,
                        MachineTypeOffering.region == item.region,
                    )
                    for item in request.regions
                )
            )
        )
    if request.zones:
        query = query.where(
            or_(
                *(
                    and_(
                        MachineType.provider == item.provider,
                        MachineTypeOffering.region == item.region,
                        MachineTypeOffering.zone == item.zone,
                    )
                    for item in request.zones
                )
            )
        )
    offerings = list(db.execute(query))
    ids = {machine.id for machine, _ in offerings}
    if not ids:
        return []

    # Mỗi scope chọn bản ghi mới nhất có hiệu lực; id phá hòa timestamp.
    prices: dict[tuple[int, str, str | None, str], SpotPrice] = {}
    for row in db.scalars(
        select(SpotPrice)
        .where(SpotPrice.machine_type_id.in_(ids), SpotPrice.recorded_at <= now)
        .order_by(SpotPrice.recorded_at.desc(), SpotPrice.id.desc())
    ):
        prices.setdefault(
            (row.machine_type_id, row.region, row.zone, row.price_type), row
        )
    risks: dict[tuple[int, str, str | None], SpotRisk] = {}
    for row_risk in db.scalars(
        select(SpotRisk)
        .where(SpotRisk.machine_type_id.in_(ids), SpotRisk.observed_at <= now)
        .order_by(SpotRisk.observed_at.desc(), SpotRisk.id.desc())
    ):
        risks.setdefault(
            (row_risk.machine_type_id, row_risk.region, row_risk.zone), row_risk
        )

    candidates = []
    for machine, offering in offerings:
        if request.region in REGION_GROUPS and offering.region != REGION_GROUPS[
            request.region
        ].get(machine.provider):
            continue
        scope = (machine.id, offering.region, offering.zone)
        regional_scope = (machine.id, offering.region, None)
        key = (machine.provider, offering.region, offering.zone, machine.name)
        if allowed_keys is not None and key not in allowed_keys:
            continue
        until = (exclusions or {}).get(key)
        if until is not None and _utc(until) > now:
            continue
        # Giá theo zone ưu tiên hơn giá region; không mượn giá từ zone khác.
        spot = prices.get((*scope, "SPOT")) or prices.get((*regional_scope, "SPOT"))
        if spot is None or spot.price_per_hour < 0:
            continue
        demand = prices.get((*scope, "ON_DEMAND")) or prices.get(
            (*regional_scope, "ON_DEMAND")
        )
        risk_row = risks.get(scope) or risks.get(regional_scope)
        risk_value = default_risk
        source, metric, observed = "DEFAULT", "POLICY_SCORE", None
        if risk_row is not None and (
            risk_row.rate_max is not None or risk_row.rate_min is not None
        ):
            if risk_row.rate_max is not None:
                risk_value = risk_row.rate_max
            elif risk_row.rate_min is not None:
                # Bucket mở không có cận trên dùng điểm rủi ro cao nhất.
                risk_value = Decimal(1)
            source, metric = risk_row.source, risk_row.metric
            observed = risk_row.observed_at
        demand_price = demand.price_per_hour if demand is not None else None
        candidate = PlacementCandidate(
            machine_type_id=machine.id,
            provider=machine.provider,
            machine_type=machine.name,
            region=offering.region,
            zone=offering.zone,
            vcpu=machine.vcpu,
            ram_gib=machine.ram_gib,
            gpu_count=machine.gpu_count,
            spot_price_per_hour=spot.price_per_hour,
            price_recorded_at=spot.recorded_at,
            on_demand_price_per_hour=demand_price,
            savings_percent=(
                (1 - spot.price_per_hour / demand_price) * 100
                if demand_price is not None and demand_price > 0
                else None
            ),
            risk=risk_value,
            risk_source=source,
            risk_metric=metric,
            risk_observed_at=observed,
            quota_checked=quota_available is not None,
        )
        if quota_available is None or quota_available(candidate):
            candidates.append(candidate)

    if not candidates:
        return []
    low_price = min(c.spot_price_per_hour for c in candidates)
    price_span = max(c.spot_price_per_hour for c in candidates) - low_price
    low_risk = min(c.risk for c in candidates)
    risk_span = max(c.risk for c in candidates) - low_risk
    weight = WEIGHTS[request.optimization_mode]
    for candidate in candidates:
        candidate.normalized_price = (
            (candidate.spot_price_per_hour - low_price) / price_span
            if price_span
            else Decimal(0)
        )
        candidate.normalized_risk = (
            (candidate.risk - low_risk) / risk_span if risk_span else Decimal(0)
        )
        candidate.score = (
            weight * candidate.normalized_price
            + (1 - weight) * candidate.normalized_risk
        )
    candidates.sort(key=lambda c: (c.score, c.risk, c.spot_price_per_hour, c.key))
    return candidates[: request.limit]
