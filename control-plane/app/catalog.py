"""Catalog: look up and maintain normalized AWS/GCP machine types."""

from dataclasses import dataclass
from decimal import Decimal

from app.models import MachineType
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
    limit: int = 100,
    offset: int = 0,
) -> list[MachineType]:
    """Filter CPU/RAM/GPU requirements; does not check price or Spot capacity."""
    query = select(MachineType)
    if vcpu is not None:
        query = query.where(MachineType.vcpu >= vcpu)
    if ram_gib is not None:
        query = query.where(MachineType.ram_gib >= ram_gib)
    query = query.where(MachineType.gpu_count >= gpu_count)
    if provider is not None:
        query = query.where(MachineType.provider == provider.upper())
    query = query.order_by(MachineType.provider, MachineType.name)
    return list(db.scalars(query.offset(offset).limit(limit)))


def upsert_machine_types(db: Session, specs: list[MachineSpec]) -> dict[str, int]:
    """Insert new specs or update existing ones. Caller controls transaction/commit."""
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
