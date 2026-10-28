from app.adapters.base import CreateInstanceSpec
from app.models import SSHKey, User
from sqlalchemy import select
from sqlalchemy.orm import Session


def build_instance_spec(
    db: Session,
    *,
    user_id: int,
    machine_id: int,
    name: str,
    machine_type: str,
    image_id: str,
    zone: str,
    request_id: str,
    disk_size_gb: int = 30,
    gpu_type: str | None = None,
    gpu_count: int = 0,
    startup_script: str = "",
) -> CreateInstanceSpec:
    user = db.get(User, user_id)

    if user is None or not user.is_active:
        raise ValueError("Tài khoản không tồn tại hoặc đã bị vô hiệu hóa")

    keys = tuple(
        db.scalars(
            select(SSHKey.public_key)
            .where(SSHKey.user_id == user.id)
            .order_by(SSHKey.id)
        )
    )

    if not keys:
        raise ValueError("Hãy đăng ký SSH public key trước khi tạo máy")

    return CreateInstanceSpec(
        machine_id=machine_id,
        name=name,
        machine_type=machine_type,
        image_id=image_id,
        zone=zone,
        request_id=request_id,
        ssh_public_keys=keys,
        disk_size_gb=disk_size_gb,
        gpu_type=gpu_type,
        gpu_count=gpu_count,
        startup_script=startup_script,
    )
