"""EC2 user data for images with cloud-init enabled."""

import hashlib
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.adapters.base import CreateInstanceSpec


def build_aws_user_data(spec: CreateInstanceSpec) -> str:
    config = {
        "users": [
            {
                "name": spec.ssh_username,
                "shell": "/bin/bash",
                "lock_passwd": True,
                "sudo": ["ALL=(ALL) NOPASSWD:ALL"],
                "ssh_authorized_keys": list(spec.ssh_public_keys),
            }
        ],
        "ssh_pwauth": False,
        "disable_root": True,
        "allow_public_ssh_keys": False,
    }

    # JSON is valid YAML and safely escapes all data inserted into cloud-config.
    cloud_config = "#cloud-config\n" + json.dumps(config, ensure_ascii=True) + "\n"

    # Stable MIME boundaries are required for EC2 ClientToken retries.
    digest = hashlib.sha256(
        (cloud_config + spec.startup_script).encode("utf-8")
    ).hexdigest()

    message = MIMEMultipart(boundary="spulti-" + digest[:48])
    message.attach(MIMEText(cloud_config, "cloud-config", "us-ascii"))

    if spec.startup_script:
        script = spec.startup_script.replace("\r\n", "\n")
        if not script.startswith("#!"):
            raise ValueError(
                "startup_script phải bắt đầu bằng shebang, ví dụ #!/bin/bash"
            )
        message.attach(MIMEText(script, "x-shellscript", "utf-8"))

    user_data = message.as_string()
    if len(user_data.encode("utf-8")) > 16 * 1024:
        raise ValueError(
            "AWS user data vượt giới hạn 16 KiB; giảm key hoặc startup_script"
        )
    return user_data
