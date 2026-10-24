import boto3
from app.adapters.base import (
    CloudAdapter,
    CreateInstanceSpec,
    InstanceInfo,
)


class AWSAdapter(CloudAdapter):
    def __init__(
        self,
        region: str,
        subnet_id: str,
        security_group_ids: list[str],
        key_name: str,
    ):
        # boto3 tự tìm credentials từ môi trường hoặc IAM role.
        self.ec2 = boto3.client("ec2", region_name=region)
        self.subnet_id = subnet_id
        self.security_group_ids = security_group_ids

        # Tên EC2 key pair đã tồn tại trong region.
        self.key_name = key_name

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
            KeyName=self.key_name,
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
            UserData=spec.startup_script,
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
