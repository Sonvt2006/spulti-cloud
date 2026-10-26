# Nạp dữ liệu minh họa cho đủ 9 bảng trong app/models.py.
# Tạo bảng trước bằng python -m app.init_db, sau đó chạy: python -m app.seed
# Chạy lại không sao: nếu user demo đã có thì bỏ qua, không thêm gì.
# User demo bị vô hiệu hóa, không dùng để đăng nhập. Khóa SSH/API chỉ để
# minh họa; API key đã bị thu hồi. Giá và chi phí không phải báo giá thật.
from datetime import UTC, datetime
from decimal import Decimal

from app.database import SessionLocal
from app.models import (
    APIKey,
    CostRecord,
    Event,
    Instance,
    Machine,
    MachineType,
    SpotPrice,
    SSHKey,
    User,
)
from app.services.catalog import MachineSpec, upsert_machine_types
from sqlalchemy import select
from sqlalchemy.orm import Session

DEMO_USERNAME = "spulti_demo"


def _at(hour: int, minute: int = 0) -> datetime:
    # Mọi mốc thời gian demo đều trong ngày 2026-10-20, giờ UTC.
    return datetime(2026, 10, 20, hour, minute, tzinfo=UTC)


def seed(db: Session) -> bool:
    # Trả về False nếu dữ liệu demo đã có từ trước.
    if db.scalar(select(User).where(User.username == DEMO_USERNAME)):
        return False

    user = User(
        name="Spulti Demo",
        email="demo@example.com",
        username=DEMO_USERNAME,
        password_hash="!seed-account-disabled",
        is_active=False,
    )
    user.ssh_keys.append(
        SSHKey(
            name="demo-laptop",
            public_key=(
                "ssh-ed25519 "
                "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"
                " demo@example.com"
            ),
            fingerprint="SHA256:ZkAslGjFiUHdGf/WUL8rQvkib4PTvQatUV0OUQSncCA",
        )
    )
    user.api_keys.append(
        APIKey(
            name="demo-cli-revoked",
            key_prefix="spulti_seed",
            key_hash="bdab6b6d545d7de49c757ca27068ad3db9df833eab7455521dcc0d159cb31061",
            last_used_at=_at(8),
            expires_at=datetime(2026, 10, 21, tzinfo=UTC),
            revoked_at=_at(9),
        )
    )
    db.add(user)
    db.flush()

    # Loại máy có thể đã có sẵn trong catalog, nên dùng upsert thay vì thêm mới.
    upsert_machine_types(
        db,
        [
            MachineSpec("AWS", "t3.medium", 2, Decimal("4.00")),
            MachineSpec("GCP", "e2-medium", 2, Decimal("4.00")),
        ],
    )
    aws_type = db.scalar(
        select(MachineType).where(
            MachineType.provider == "AWS", MachineType.name == "t3.medium"
        )
    )
    gcp_type = db.scalar(
        select(MachineType).where(
            MachineType.provider == "GCP", MachineType.name == "e2-medium"
        )
    )

    aws_machine = Machine(
        user_id=user.id,
        name="demo-training-aws",
        requested_vcpu=2,
        requested_ram_gib=Decimal("4.00"),
        requested_gpu_count=0,
        optimization_mode="SAVING",
        status="RUNNING",
        created_at=_at(7, 55),
    )
    gcp_machine = Machine(
        user_id=user.id,
        name="demo-api-gcp",
        requested_vcpu=2,
        requested_ram_gib=Decimal("4.00"),
        requested_gpu_count=0,
        optimization_mode="STABLE",
        status="RUNNING",
        created_at=_at(7, 55),
    )
    db.add_all([aws_machine, gcp_machine])
    # Machine và Instance không khai báo relationship, cần flush để có ID.
    db.flush()

    # Instance AWS đầu tiên bị thu hồi lúc 09:00 UTC.
    aws_old = Instance(
        machine_id=aws_machine.id,
        machine_type_id=aws_type.id,
        provider_instance_id="i-seed-aws-old",
        region="ap-southeast-1",
        zone="ap-southeast-1a",
        started_at=_at(8),
        ended_at=_at(9),
    )
    # Instance thay thế vẫn thuộc cùng máy logic demo-training-aws.
    aws_new = Instance(
        machine_id=aws_machine.id,
        machine_type_id=aws_type.id,
        provider_instance_id="i-seed-aws-new",
        region="ap-southeast-1",
        zone="ap-southeast-1b",
        started_at=_at(9, 5),
    )
    gcp_api = Instance(
        machine_id=gcp_machine.id,
        machine_type_id=gcp_type.id,
        provider_instance_id="seed-gcp-api-01",
        region="asia-southeast1",
        zone="asia-southeast1-a",
        started_at=_at(8),
    )
    db.add_all([aws_old, aws_new, gcp_api])
    db.flush()

    db.add_all(
        [
            SpotPrice(
                machine_type_id=aws_type.id,
                region="ap-southeast-1",
                zone="ap-southeast-1a",
                price_type="SPOT",
                price_per_hour=Decimal("0.015000"),
                recorded_at=_at(8),
            ),
            SpotPrice(
                machine_type_id=aws_type.id,
                region="ap-southeast-1",
                zone="ap-southeast-1b",
                price_type="SPOT",
                price_per_hour=Decimal("0.016000"),
                recorded_at=_at(9),
            ),
            SpotPrice(
                machine_type_id=aws_type.id,
                region="ap-southeast-1",
                zone=None,
                price_type="ON_DEMAND",
                price_per_hour=Decimal("0.050000"),
                recorded_at=_at(8),
            ),
            SpotPrice(
                machine_type_id=gcp_type.id,
                region="asia-southeast1",
                zone="asia-southeast1-a",
                price_type="SPOT",
                price_per_hour=Decimal("0.012000"),
                recorded_at=_at(8),
            ),
            SpotPrice(
                machine_type_id=gcp_type.id,
                region="asia-southeast1",
                zone=None,
                price_type="ON_DEMAND",
                price_per_hour=Decimal("0.040000"),
                recorded_at=_at(8),
            ),
            Event(
                machine_id=aws_machine.id,
                instance_id=aws_old.id,
                event_type="RECLAIM",
                details={
                    "message": "Demo spot instance reclaimed",
                    "replacement_instance_id": "i-seed-aws-new",
                },
                created_at=_at(9),
            ),
            # Chi phí minh họa tính đến 10:05 UTC: AWS cũ 1 giờ, AWS mới 1 giờ,
            # GCP 2 giờ 5 phút chạy ON_DEMAND (máy chọn chế độ STABLE).
            CostRecord(
                instance_id=aws_old.id,
                actual_cost=Decimal("0.015000"),
                on_demand_cost=Decimal("0.050000"),
                calculated_at=_at(10, 5),
            ),
            CostRecord(
                instance_id=aws_new.id,
                actual_cost=Decimal("0.016000"),
                on_demand_cost=Decimal("0.050000"),
                calculated_at=_at(10, 5),
            ),
            CostRecord(
                instance_id=gcp_api.id,
                actual_cost=Decimal("0.083333"),
                on_demand_cost=Decimal("0.083333"),
                calculated_at=_at(10, 5),
            ),
        ]
    )
    return True


def main() -> None:
    # Tất cả nằm trong một transaction: lỗi ở đâu thì không bảng nào bị ghi.
    with SessionLocal.begin() as db:
        created = seed(db)
    print("Seeded demo data." if created else "Demo data already exists, skipped.")


if __name__ == "__main__":
    main()
