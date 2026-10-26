from datetime import datetime
from decimal import Decimal
from typing import Any

from app.database import Base
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship


def _id_pk() -> Mapped[int]:
    # ID là số tự tăng: 1, 2, 3, ...
    return mapped_column(Integer, primary_key=True)


def _created_at() -> Mapped[datetime]:
    # Thời điểm tạo, database tự điền.
    return mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


# ---------------------------------------------------------------------------
# Người dùng và xác thực
# ---------------------------------------------------------------------------


class User(Base):
    # Tài khoản người dùng.
    __tablename__ = "users"

    id: Mapped[int] = _id_pk()
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    ssh_keys: Mapped[list["SSHKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    api_keys: Mapped[list["APIKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )


class SSHKey(Base):
    # Khóa SSH công khai, được cài vào mọi máy của người dùng.
    __tablename__ = "ssh_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "name"),
        UniqueConstraint("user_id", "fingerprint"),
    )

    id: Mapped[int] = _id_pk()
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Mã nhận dạng khóa, ví dụ "SHA256:...".
    fingerprint: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    user: Mapped[User] = relationship(back_populates="ssh_keys")


class APIKey(Base):
    # Khóa để CLI spulti đăng nhập.
    __tablename__ = "api_keys"

    id: Mapped[int] = _id_pk()
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Không lưu khóa gốc, chỉ lưu vài ký tự đầu và mã băm.
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_keys")


# ---------------------------------------------------------------------------
# Máy, instance, giá, sự kiện, chi phí
# ---------------------------------------------------------------------------


class MachineType(Base):
    # Danh sách loại máy của AWS và GCP.
    __tablename__ = "machine_types"
    __table_args__ = (
        UniqueConstraint("provider", "name"),
        CheckConstraint(
            "provider IN ('AWS', 'GCP')", name="machine_types_provider_check"
        ),
        CheckConstraint("vcpu > 0", name="machine_types_vcpu_check"),
        CheckConstraint("ram_gib > 0", name="machine_types_ram_gib_check"),
    )

    id: Mapped[int] = _id_pk()
    provider: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    vcpu: Mapped[int] = mapped_column(Integer, nullable=False)
    ram_gib: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    gpu_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


class Machine(Base):
    # Máy mà người dùng nhìn thấy, giữ nguyên tên dù bị thay máy thật.
    __tablename__ = "machines"
    __table_args__ = (
        CheckConstraint(
            "optimization_mode IN ('SAVING', 'STABLE', 'BALANCED')",
            name="machines_optimization_mode_check",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROVISIONING', 'RUNNING', 'RECOVERING', 'DELETED')",
            name="machines_status_check",
        ),
        CheckConstraint("requested_gpu_count >= 0", name="machines_gpu_count_check"),
    )

    id: Mapped[int] = _id_pk()
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_vcpu: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_ram_gib: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    requested_gpu_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    requested_gpu_type: Mapped[str | None] = mapped_column(String(100))
    requested_disk_gib: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    requested_region: Mapped[str | None] = mapped_column(String(50))
    requested_zone: Mapped[str | None] = mapped_column(String(50))
    optimization_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'")
    )
    created_at: Mapped[datetime] = _created_at()


class Instance(Base):
    # Máy thật trên AWS/GCP. Mỗi lần bị thu hồi sẽ có instance mới.
    __tablename__ = "instances"
    __table_args__ = (UniqueConstraint("machine_type_id", "provider_instance_id"),)

    id: Mapped[int] = _id_pk()
    machine_id: Mapped[int] = mapped_column(
        ForeignKey("machines.id"), nullable=False, index=True
    )
    machine_type_id: Mapped[int] = mapped_column(
        ForeignKey("machine_types.id"), nullable=False
    )
    provider_instance_id: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str] = mapped_column(String(50), nullable=False)
    zone: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SpotPrice(Base):
    # Lịch sử giá theo giờ của từng loại máy.
    __tablename__ = "spot_prices"
    __table_args__ = (
        CheckConstraint(
            "price_type IN ('SPOT', 'ON_DEMAND')", name="spot_prices_price_type_check"
        ),
        CheckConstraint("price_per_hour >= 0", name="spot_prices_price_per_hour_check"),
    )

    id: Mapped[int] = _id_pk()
    machine_type_id: Mapped[int] = mapped_column(
        ForeignKey("machine_types.id"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(50), nullable=False)
    zone: Mapped[str | None] = mapped_column(String(50))
    price_type: Mapped[str] = mapped_column(String(20), nullable=False)
    price_per_hour: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Event(Base):
    # Sự kiện xảy ra với máy, ví dụ bị thu hồi.
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('RECLAIM', 'INTERRUPT')", name="events_event_type_check"
        ),
    )

    id: Mapped[int] = _id_pk()
    machine_id: Mapped[int] = mapped_column(
        ForeignKey("machines.id"), nullable=False, index=True
    )
    instance_id: Mapped[int | None] = mapped_column(ForeignKey("instances.id"))
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()


class CostRecord(Base):
    # Chi phí của mỗi instance.
    __tablename__ = "cost_records"

    id: Mapped[int] = _id_pk()
    instance_id: Mapped[int] = mapped_column(
        ForeignKey("instances.id"), nullable=False, unique=True
    )
    actual_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    on_demand_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, server_default=text("0")
    )
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SpotRisk(Base):
    """History of AWS interruption buckets and GCP daily preemption rates."""

    __tablename__ = "spot_risks"
    __table_args__ = (
        CheckConstraint(
            "rate_min IS NULL OR (rate_min >= 0 AND rate_min <= 1)",
            name="spot_risks_min_check",
        ),
        CheckConstraint(
            "rate_max IS NULL OR (rate_max >= 0 AND rate_max <= 1)",
            name="spot_risks_max_check",
        ),
    )

    id: Mapped[int] = _id_pk()
    machine_type_id: Mapped[int] = mapped_column(
        ForeignKey("machine_types.id"), nullable=False
    )
    region: Mapped[str] = mapped_column(String(50), nullable=False)
    zone: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    metric: Mapped[str] = mapped_column(String(50), nullable=False)
    rate_min: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    rate_max: Mapped[Decimal | None] = mapped_column(Numeric(8, 6))
    label: Mapped[str | None] = mapped_column(String(50))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
