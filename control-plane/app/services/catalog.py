"""Catalog: tra cứu và cập nhật danh sách loại máy AWS/GCP đã chuẩn hóa."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.models import MachineType, MachineTypeOffering
from sqlalchemy import select
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class MachineSpec:
    provider: str
    name: str
    vcpu: int
    ram_gib: Decimal
    gpu_count: int = 0


def find_machine_types(
    db: Session,
    *,
    vcpu: int | None = None,
    ram_gib: Decimal | None = None,
    gpu_count: int = 0,
    provider: str | None = None,
    region: str | None = None,
    zone: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[MachineType]:
    """Lọc theo yêu cầu CPU/RAM/GPU; không kiểm tra giá hay còn máy Spot không."""
    query = select(MachineType)
    if vcpu is not None:
        query = query.where(MachineType.vcpu >= vcpu)
    if ram_gib is not None:
        query = query.where(MachineType.ram_gib >= ram_gib)
    query = query.where(MachineType.gpu_count >= gpu_count)
    if provider is not None:
        query = query.where(MachineType.provider == provider.upper())
    query = query.order_by(MachineType.provider, MachineType.name)
    if region is not None or zone is not None:
        conditions = [MachineTypeOffering.machine_type_id == MachineType.id]
    if region is not None:
        conditions.append(MachineTypeOffering.region == region)
    if zone is not None:
        conditions.append(MachineTypeOffering.zone == zone)
    query = query.where(select(MachineTypeOffering.id).where(*conditions).exists())
    return list(db.scalars(query.offset(offset).limit(limit)))


def upsert_machine_types(db: Session, specs: list[MachineSpec]) -> dict[str, int]:
    """Thêm loại máy mới hoặc cập nhật loại đã có. Nơi gọi tự lo transaction/commit."""
    counts = {"created": 0, "updated": 0}
    for spec in specs:
        provider = spec.provider.upper()
        if provider not in {"AWS", "GCP"}:
            raise ValueError(f"Unsupported provider: {provider}")
        if not spec.name or spec.vcpu <= 0 or spec.ram_gib <= 0 or spec.gpu_count < 0:
            raise ValueError(f"Invalid machine specification: {spec}")

        current = db.scalar(
            select(MachineType).where(
                MachineType.provider == provider, MachineType.name == spec.name
            )
        )
        if current is None:
            db.add(
                MachineType(
                    provider=provider,
                    name=spec.name,
                    vcpu=spec.vcpu,
                    ram_gib=spec.ram_gib,
                    gpu_count=spec.gpu_count,
                )
            )
            counts["created"] += 1
        else:
            current.vcpu = spec.vcpu
            current.ram_gib = spec.ram_gib
            current.gpu_count = spec.gpu_count
            counts["updated"] += 1
        db.flush()
    return counts


def upsert_machine_type_offerings(
    db: Session,
    *,
    provider: str,
    region: str,
    zone: str,
    machine_names: list[str],
) -> dict[str, int]:
    """Record zone-level type offerings. Caller controls transaction/commit."""
    if not region or not zone:
        raise ValueError("Expected a region and a zone")
    provider = provider.upper()
    if provider not in {"AWS", "GCP"}:
        raise ValueError(f"Unsupported provider: {provider}")
    names = set(machine_names)
    if not names:
        return {"created": 0, "refreshed": 0}

    ids = {
        machine.name: machine.id
        for machine in db.scalars(
            select(MachineType).where(
                MachineType.provider == provider,
                MachineType.name.in_(names),
            )
        )
    }
    unknown = names - ids.keys()
    if unknown:
        raise ValueError(f"Machine types missing from catalog: {sorted(unknown)}")

    existing = {
        record.machine_type_id: record
        for record in db.scalars(
            select(MachineTypeOffering).where(
                MachineTypeOffering.region == region,
                MachineTypeOffering.zone == zone,
                MachineTypeOffering.machine_type_id.in_(ids.values()),
            )
        )
    }
    counts = {"created": 0, "refreshed": 0}
    now = datetime.now(UTC)
    for name in sorted(names):
        machine_id = ids[name]
        if machine_id in existing:
            existing[machine_id].last_seen_at = now
            counts["refreshed"] += 1
        else:
            db.add(
                MachineTypeOffering(
                    machine_type_id=machine_id,
                    region=region,
                    zone=zone,
                    last_seen_at=now,
                )
            )
            counts["created"] += 1
    db.flush()
    return counts


def sync_catalog_aws(region: str, zone: str, limit_types: int | None = None):
    """Discover type offerings for one AWS Zone and persist normalized specs."""
    from app.adapters.aws import AWSAdapter
    from app.database import SessionLocal

    names = AWSAdapter.offered_machine_types_in_zone(region, zone)
    if limit_types is not None:
        names = names[:limit_types]
    specs = AWSAdapter.describe_machine_specs(region, names) if names else []
    with SessionLocal.begin() as db:
        upsert_machine_types(db, specs)
        result = upsert_machine_type_offerings(
            db, provider="AWS", region=region, zone=zone, machine_names=names
        )
    return {"machine_types": len(names), **result}


def sync_catalog_gcp(
    project_id: str, region: str, zone: str, limit_types: int | None = None
):
    """Discover type offerings for one GCP Zone and persist normalized specs."""
    from app.adapters.gcp import GCPAdapter
    from app.database import SessionLocal

    names = GCPAdapter.offered_machine_types(project_id, zone)
    if limit_types is not None:
        names = names[:limit_types]
    specs = GCPAdapter.describe_machine_specs(project_id, zone, names) if names else []
    with SessionLocal.begin() as db:
        upsert_machine_types(db, specs)
        result = upsert_machine_type_offerings(
            db, provider="GCP", region=region, zone=zone, machine_names=names
        )
    return {"machine_types": len(names), **result}
