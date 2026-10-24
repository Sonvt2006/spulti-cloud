import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.adapters.base import CreateInstanceSpec
from app.adapters.gcp import GCPAdapter

# Giữ tên này cho toàn bộ vòng create → status → delete.
INSTANCE_NAME = "spulti-gcp-test-01"
SSH_PUBLIC_KEY_PATH = Path("/app-keys/spulti-gcp-test.pub")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Chưa đặt biến môi trường {name}")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["create", "status", "delete"],
    )
    args = parser.parse_args()

    project = "spulti-cloud"
    region = "asia-southeast1"
    zone = "asia-southeast1-b"
    subnet_name = "spulti-test-subnet"

    if not zone.startswith(region + "-"):
        raise ValueError("Zone không thuộc region đã chọn")

    subnet = f"projects/{project}/regions/{region}" f"/subnetworks/{subnet_name}"

    adapter = GCPAdapter(
        project_id=project,
        zone=zone,
        subnetwork=subnet,
        ssh_username="spulti",
        ssh_public_key=SSH_PUBLIC_KEY_PATH.read_text(encoding="utf-8").strip(),
    )

    if args.action == "create":
        # Cùng project/zone/name tạo cùng request ID.
        # Khi retry, giữ nguyên toàn bộ cấu hình.
        request_id = str(
            uuid5(
                NAMESPACE_URL,
                f"spulti-test/{project}/{zone}/{INSTANCE_NAME}",
            )
        )

        spec = CreateInstanceSpec(
            machine_id=0,
            name=INSTANCE_NAME,
            machine_type="e2-standard-2",
            image_id="https://www.googleapis.com/compute/v1/projects/ubuntu-os-cloud/global/images/ubuntu-2204-jammy-v20261021",
            zone=zone,
            request_id=request_id,
            disk_size_gb=20,
            startup_script=(
                "#!/bin/bash\n"
                "mkdir -p /workspace\n"
                "echo 'GCP adapter OK' > /workspace/test.txt\n"
            ),
        )

        print("Project:", project)
        print("Zone:", zone)
        print("Subnet:", subnet)
        print("Name:", spec.name)
        print("Image:", spec.image_id)
        print("Đang tạo Spot VM...", flush=True)

        result = adapter.create_instance(spec)
        print(json.dumps(asdict(result), indent=2))

    elif args.action == "status":
        result = adapter.get_instance(INSTANCE_NAME)
        print(json.dumps(asdict(result), indent=2))

    else:
        adapter.delete_instance(INSTANCE_NAME)
        print(f"Đã xóa VM: {INSTANCE_NAME}")


if __name__ == "__main__":
    main()
