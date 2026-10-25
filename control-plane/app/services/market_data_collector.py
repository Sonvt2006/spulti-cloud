from datetime import UTC, datetime, timedelta
from decimal import Decimal

import boto3
from app.database import SessionLocal
from app.models import MachineType, SpotPrice
from sqlalchemy import select


def collect_aws_spot_price(
    region="us-east-1",
    zone="us-east-1a",
    instance_type="t3.medium",
):
    ec2 = boto3.client("ec2", region_name=region)
    now = datetime.now(UTC)

    response = ec2.describe_spot_price_history(
        InstanceTypes=[instance_type],
        ProductDescriptions=["Linux/UNIX"],
        AvailabilityZone=zone,
        StartTime=now - timedelta(hours=1),
        EndTime=now,
    )

    prices = response["SpotPriceHistory"]
    if not prices:
        raise RuntimeError("Không tìm thấy giá Spot")

    latest = max(prices, key=lambda x: x["Timestamp"])

    with SessionLocal.begin() as db:
        machine = db.scalar(
            select(MachineType).where(
                MachineType.provider == "AWS",
                MachineType.name == instance_type,
            )
        )

        if machine is None:
            raise ValueError("Loại máy chưa có trong Catalog")

        db.add(
            SpotPrice(
                machine_type_id=machine.id,
                region=region,
                zone=zone,
                price_type="SPOT",
                price_per_hour=Decimal(latest["SpotPrice"]),
                recorded_at=now,
            )
        )

    print(f"Đã lưu {instance_type}: {latest['SpotPrice']} USD/h")


if __name__ == "__main__":
    collect_aws_spot_price()
