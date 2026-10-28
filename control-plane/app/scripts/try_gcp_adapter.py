import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.adapters.base import CreateInstanceSpec
from app.adapters.gcp import GCPAdapter
from app.database import SessionLocal
from app.services.instance_manager import build_instance_spec

# Giữ tên này cho toàn bộ vòng create → status → delete.
INSTANCE_NAME = "spulti-gcp-long-test-01"
CREATE_GENERATION = "01"
STATE_FILE = Path("/var/lib/spulti-tests/gcp-adapter-test.json")


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
    parser.add_argument("--user-id", type=int, help="ID tài khoản trong database")
    args = parser.parse_args()

    project = "spulti-cloud"
    region = "asia-southeast1"
    zone = "asia-southeast1-b"
    subnet_name = "spulti-test-subnet"

    if not zone.startswith(region + "-"):
        raise ValueError("Zone không thuộc region đã chọn")

    subnet = f"projects/{project}/regions/{region}/subnetworks/{subnet_name}"

    adapter = GCPAdapter(
        project_id=project,
        zone=zone,
        subnetwork=subnet,
    )

    if args.action == "create":
        if args.user_id is None:
            parser.error("create yêu cầu --user-id của tài khoản đã đăng ký public key")
        # Cùng project/zone/name tạo cùng request ID.
        # Khi retry, giữ nguyên toàn bộ cấu hình.
        request_id = str(
            uuid5(
                NAMESPACE_URL,
                f"spulti-test/{project}/{zone}/{INSTANCE_NAME}"
                f"/{args.user_id}/{CREATE_GENERATION}",
            )
        )

        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if (
                state["user_id"] != args.user_id
                or state["project"] != project
                or state["subnet"] != subnet
                or state["spec"]["request_id"] != request_id
            ):
                raise ValueError(
                    "State không khớp cấu hình. Kiểm tra/xóa VM cũ rồi lưu trữ "
                    "file state trước khi tạo mới."
                )
            spec = CreateInstanceSpec(**state["spec"])
        else:
            with SessionLocal() as db:
                spec = build_instance_spec(
                    db,
                    user_id=args.user_id,
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

            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps(
                    {
                        "user_id": args.user_id,
                        "project": project,
                        "subnet": subnet,
                        "spec": asdict(spec),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

        print("Project:", project)
        print("Zone:", zone)
        print("Subnet:", subnet)
        print("Name:", spec.name)
        print("Image:", spec.image_id)
        print("Đang tạo Spot VM...", flush=True)

        result = adapter.create_instance(spec)
        print(json.dumps(asdict(result), indent=2))
        print(f"SSH: ssh {spec.ssh_username}@{result.public_ip or '<PUBLIC_IP>'}")

    elif args.action == "status":
        result = adapter.get_instance(INSTANCE_NAME)
        print(json.dumps(asdict(result), indent=2))

    else:
        adapter.delete_instance(INSTANCE_NAME)
        print(f"Đã xóa VM: {INSTANCE_NAME}")


if __name__ == "__main__":
    main()
