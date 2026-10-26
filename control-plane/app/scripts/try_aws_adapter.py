import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from app.adapters.aws import AWSAdapter
from app.adapters.base import CreateInstanceSpec

STATE_FILE = Path("/var/lib/spulti-tests/aws-adapter-test.json")


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Chưa đặt biến môi trường {name}")
    return value


def new_state() -> dict:
    request_id = str(uuid4())

    spec = CreateInstanceSpec(
        machine_id=0,  # Test độc lập, không ghi DB.
        name=f"spulti-test-{request_id[:8]}",
        machine_type="t3.small",
        image_id="ami-033ea4ff4a9c726dc",  # Ubuntu Server 22.04 LTS
        zone="us-east-1c",
        request_id=request_id,
        disk_size_gb=10,
        startup_script=(
            "#!/bin/bash\n"
            "mkdir -p /workspace\n"
            "echo 'AWS adapter test OK' > /workspace/test.txt\n"
        ),
    )

    return {
        "region": "us-east-1",
        "subnet_id": "subnet-0ceb087f813c4bf6c",
        "security_group_ids": ["sg-018c6610dc3cbedbe"],
        "key_name": "spulti-key",
        "spec": asdict(spec),
        "instance_id": None,
    }


def make_adapter(state: dict) -> AWSAdapter:
    return AWSAdapter(
        region=state["region"],
        subnet_id=state["subnet_id"],
        security_group_ids=state["security_group_ids"],
        key_name=state["key_name"],
    )


def show(info) -> None:
    print(json.dumps(asdict(info), indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["create", "status", "delete"],
    )
    args = parser.parse_args()

    if args.action == "create":
        if STATE_FILE.exists():
            state = load_state()
        else:
            state = new_state()
            save_state(state)

        adapter = make_adapter(state)

        if state["instance_id"]:
            print("Đã có instance trong file trạng thái:")
            show(adapter.get_instance(state["instance_id"]))
            return

        print("Đang gửi yêu cầu tạo Spot Instance...")
        print("Name:", state["spec"]["name"])
        print("Request ID:", state["spec"]["request_id"])

        result = adapter.create_instance(CreateInstanceSpec(**state["spec"]))

        # Lưu ID ngay sau khi nhận kết quả.
        state["instance_id"] = result.instance_ref
        save_state(state)
        show(result)

        print("\nĐã lưu instance ID. Dùng lệnh status để kiểm tra.")
        return

    if not STATE_FILE.exists():
        raise SystemExit("Chưa có file trạng thái. Chạy create trước.")

    state = load_state()
    instance_id = state.get("instance_id")

    if not instance_id:
        raise SystemExit(
            "Chưa lưu được instance ID. Kiểm tra EC2 Console "
            "theo tên máy trong file trạng thái trước khi thử tiếp."
        )

    adapter = make_adapter(state)

    if args.action == "status":
        show(adapter.get_instance(instance_id))

    elif args.action == "delete":
        adapter.delete_instance(instance_id)
        print(f"Đã gửi yêu cầu terminate {instance_id}.")
        print("Dùng status hoặc EC2 Console để xác nhận terminated.")


if __name__ == "__main__":
    main()
