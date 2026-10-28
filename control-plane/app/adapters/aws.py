from decimal import Decimal
from functools import lru_cache

import boto3
from app.adapters.base import (
    CloudAdapter,
    CreateInstanceSpec,
    InstanceInfo,
)
from app.adapters.cloud_init import build_aws_user_data


class AWSAdapter(CloudAdapter):
    def __init__(
        self,
        region: str,
        subnet_id: str,
        security_group_ids: list[str],
    ):
        # boto3 tự tìm credentials từ môi trường hoặc IAM role.
        self.ec2 = boto3.client("ec2", region_name=region)
        self.subnet_id = subnet_id
        self.security_group_ids = security_group_ids

    @staticmethod
    def _to_info(instance: dict) -> InstanceInfo:
        return InstanceInfo(
            provider="AWS",
            instance_ref=instance["InstanceId"],
            provider_instance_id=instance["InstanceId"],
            status=instance["State"]["Name"],
            private_ip=instance.get("PrivateIpAddress"),
            public_ip=instance.get("PublicIpAddress"),
        )

    def create_instance(
        self,
        spec: CreateInstanceSpec,
    ) -> InstanceInfo:
        user_data = build_aws_user_data(spec)
        # Đọc tên root device của AMI,
        # tránh giả định mọi image đều dùng /dev/sda1.
        images = self.ec2.describe_images(ImageIds=[spec.image_id])["Images"]

        if not images:
            raise ValueError(f"Không tìm thấy AMI: {spec.image_id}")

        image = images[0]
        if image["RootDeviceType"] != "ebs":
            raise ValueError("Mẫu này chỉ hỗ trợ AMI có EBS root disk")

        response = self.ec2.run_instances(
            ImageId=spec.image_id,
            InstanceType=spec.machine_type,
            MinCount=1,
            MaxCount=1,
            ClientToken=spec.request_id,
            Placement={
                "AvailabilityZone": spec.zone,
            },
            NetworkInterfaces=[
                {
                    "DeviceIndex": 0,
                    "SubnetId": self.subnet_id,
                    "Groups": self.security_group_ids,
                    "AssociatePublicIpAddress": True,
                    "DeleteOnTermination": True,
                }
            ],
            InstanceMarketOptions={
                "MarketType": "spot",
                "SpotOptions": {
                    "SpotInstanceType": "one-time",
                    "InstanceInterruptionBehavior": "terminate",
                },
            },
            BlockDeviceMappings=[
                {
                    "DeviceName": image["RootDeviceName"],
                    "Ebs": {
                        "VolumeSize": spec.disk_size_gb,
                        "VolumeType": "gp3",
                        "DeleteOnTermination": True,
                        "Encrypted": True,
                    },
                }
            ],
            UserData=user_data,
            MetadataOptions={
                "HttpTokens": "required",
            },
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": spec.name},
                        {
                            "Key": "spulti-machine-id",
                            "Value": str(spec.machine_id),
                        },
                    ],
                }
            ],
        )

        return self._to_info(response["Instances"][0])

    def get_instance(self, instance_ref: str) -> InstanceInfo:
        response = self.ec2.describe_instances(InstanceIds=[instance_ref])
        instance = response["Reservations"][0]["Instances"][0]
        return self._to_info(instance)

    def delete_instance(self, instance_ref: str) -> None:
        self.ec2.terminate_instances(InstanceIds=[instance_ref])

    @staticmethod
    def available_regions() -> list[str]:
        """AWS regions enabled for this account."""
        ec2 = boto3.client("ec2", region_name="us-east-1")
        result = ec2.describe_regions(AllRegions=False)
        return sorted(r["RegionName"] for r in result["Regions"])

    @staticmethod
    def offered_machine_types(region: str) -> list[str]:
        """Types offered in a region, NOT proof of free Spot capacity."""

        client = boto3.client("ec2", region_name=region)
        types = set()
        pages = client.get_paginator("describe_instance_type_offerings").paginate(
            LocationType="region"
        )

        for page in pages:
            types.update(x["InstanceType"] for x in page["InstanceTypeOfferings"])

        return sorted(types)

    @staticmethod
    def describe_machine_specs(region: str, instance_types: list[str]):
        """Return normalized specs for types found by offered_machine_types."""
        from app.services.catalog import MachineSpec

        client = boto3.client("ec2", region_name=region)
        specs = []

        for i in range(0, len(instance_types), 50):
            names = instance_types[i : i + 50]
            for page in client.get_paginator("describe_instance_types").paginate(
                InstanceTypes=names
            ):
                for item in page["InstanceTypes"]:
                    specs.append(
                        MachineSpec(
                            provider="AWS",
                            name=item["InstanceType"],
                            vcpu=item["VCpuInfo"]["DefaultVCpus"],
                            ram_gib=Decimal(item["MemoryInfo"]["SizeInMiB"])
                            / Decimal(1024),
                            gpu_count=sum(
                                gpu["Count"]
                                for gpu in item.get("GpuInfo", {}).get("Gpus", [])
                            ),
                        )
                    )
        return specs

    @staticmethod
    def get_spot_prices(region: str, instance_types: list[str]):
        from datetime import UTC, datetime, timedelta

        from app.adapters.base import SpotPriceQuote

        if not instance_types:
            return []

        ec2 = boto3.client("ec2", region_name=region)
        now = datetime.now(UTC)
        latest = {}
        paginator = ec2.get_paginator("describe_spot_price_history")

        for i in range(0, len(instance_types), 25):
            for page in paginator.paginate(
                InstanceTypes=instance_types[i : i + 25],
                ProductDescriptions=["Linux/UNIX"],
                StartTime=now - timedelta(hours=1),
                EndTime=now,
            ):
                for item in page["SpotPriceHistory"]:
                    key = (item["InstanceType"], item["AvailabilityZone"])
                    if (
                        key not in latest
                        or item["Timestamp"] > latest[key]["Timestamp"]
                    ):
                        latest[key] = item

        return [
            SpotPriceQuote(
                provider="AWS",
                machine_type=item["InstanceType"],
                region=region,
                zone=item["AvailabilityZone"],
                price_per_hour=Decimal(item["SpotPrice"]),
                effective_at=item["Timestamp"],
            )
            for item in latest.values()
        ]

    @staticmethod
    @lru_cache(maxsize=1)
    def _advisor_feed():
        import json
        from urllib.request import Request, urlopen

        url = "https://spot-bid-advisor.s3.amazonaws.com/spot-advisor-data.json"
        request = Request(url, headers={"User-Agent": "SpultiMarketCollector/1.0"})
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    @staticmethod
    def get_interruption_risks(region: str, instance_types: list[str]):
        """Region-level, 30-day AWS Advisor buckets; not an exact probability."""
        import logging
        from datetime import UTC, datetime, timedelta

        from app.adapters.base import SpotRiskEstimate

        try:
            advisor = AWSAdapter._advisor_feed()
        except (OSError, ValueError) as exc:
            logging.warning("AWS Advisor feed unavailable: %s", exc)
            return []

        linux = advisor.get("spot_advisor", {}).get(region, {}).get("Linux", {})
        ranges = advisor.get("ranges", [])
        bounds = [
            (Decimal("0"), Decimal("0.05")),
            (Decimal("0.05"), Decimal("0.10")),
            (Decimal("0.10"), Decimal("0.15")),
            (Decimal("0.15"), Decimal("0.20")),
            (Decimal("0.20"), None),
        ]
        now = datetime.now(UTC)
        results = []
        for name in instance_types:
            info = linux.get(name)
            if not isinstance(info, dict):
                continue
            index = info.get("r")
            if not isinstance(index, int) or not 0 <= index < len(bounds):
                continue
            low, high = bounds[index]
            label = (
                ranges[index].get("label", "")
                if index < len(ranges) and isinstance(ranges[index], dict)
                else None
            )
            results.append(
                SpotRiskEstimate(
                    provider="AWS",
                    machine_type=name,
                    region=region,
                    zone=None,
                    source="AWS_SPOT_ADVISOR",
                    metric="INTERRUPTION_BUCKET_30D",
                    rate_min=low,
                    rate_max=high,
                    label=label or None,
                    observed_at=now,
                    period_start=now - timedelta(days=30),
                    period_end=now,
                )
            )
        return results

    @staticmethod
    def get_market_data(
        region: str, instance_types: list[str], include_risk: bool = True
    ):
        from app.adapters.base import MarketData

        prices = AWSAdapter.get_spot_prices(region, instance_types)
        risks = (
            AWSAdapter.get_interruption_risks(region, instance_types)
            if include_risk
            else []
        )
        return MarketData(prices=prices, risks=risks)
