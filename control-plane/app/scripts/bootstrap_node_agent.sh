#!/bin/bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Dùng Ubuntu Server 24.04 cho bài test này.
apt-get update
apt-get install -y python3 python3-boto3 ca-certificates

install -d -m 755 /opt/spulti-agent

python3 - <<'PY'
import hashlib
import tarfile
import tempfile
from pathlib import Path

import boto3

bucket = "spulti-checkpoints"
key = "agent/releases/v3-files/node-agent-v3-files.tar.gz"
expected = "18f9e98f2d03ee6d773cee0cb93625d866cfa736a79f92ee9215be0656ed5d14"

with tempfile.TemporaryDirectory() as directory:
    archive_path = Path(directory) / "agent.tar.gz"

    boto3.client("s3", region_name="ap-southeast-1").download_file(
        bucket, key, str(archive_path)
    )

    actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    if actual != expected:
        raise RuntimeError("SHA256 không khớp; dừng cài đặt")

    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall("/opt/spulti-agent", filter="data")

print("BOOTSTRAP: downloaded and verified Node Agent")
PY

python3 /opt/spulti-agent/agent.py --help

touch /opt/spulti-agent/INSTALL_OK
echo "BOOTSTRAP: Node Agent installation completed"

# Interactive submission runs as the SSH user, with that user's Spulti API key.
cat > /usr/local/bin/spulti-agent <<'CLI'
#!/bin/sh
export SPULTI_CLOUD_PROVIDER="${SPULTI_CLOUD_PROVIDER:-aws}"
exec /usr/bin/python3 /opt/spulti-agent/agent.py "$@"
CLI
chmod 0755 /usr/local/bin/spulti-agent

# Tạo user riêng cho Agent.
if ! id spulti-agent >/dev/null 2>&1; then
    useradd --system \
        --home-dir /var/lib/spulti-agent \
        --shell /usr/sbin/nologin \
        spulti-agent
fi

install -d \
    -o spulti-agent \
    -g spulti-agent \
    -m 700 \
    /var/lib/spulti-agent

install -d -o spulti-agent -g spulti-agent -m 0755 /workspace/checkpoints

cat > /etc/systemd/system/spulti-node-agent.service <<'UNIT'
[Unit]
Description=Spulti Node Agent
Wants=network-online.target
After=network-online.target
ConditionPathExists=/etc/spulti-agent.env
StartLimitIntervalSec=0

[Service]
Type=simple
User=spulti-agent
Group=spulti-agent
WorkingDirectory=/opt/spulti-agent
EnvironmentFile=/etc/spulti-agent.env
Environment=PYTHONUNBUFFERED=1
Environment=SPULTI_CHECKPOINT_ROOT=/workspace/checkpoints
Environment=SPULTI_CLOUD_PROVIDER=aws
ExecStart=/usr/bin/python3 /opt/spulti-agent/agent.py --job-id ${SPULTI_JOB_ID} --total-steps ${SPULTI_TOTAL_STEPS} --checkpoint-every ${SPULTI_CHECKPOINT_EVERY} --heartbeat-every ${SPULTI_HEARTBEAT_EVERY} --step-delay 5 --state-dir /var/lib/spulti-agent
Restart=on-failure
RestartPreventExitStatus=130
RestartSec=10
TimeoutStopSec=60
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/spulti-agent /workspace/checkpoints

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable spulti-node-agent

# Chỉ chạy khi cấu hình job đã được cấp.
if test -s /etc/spulti-agent.env; then
    systemctl start spulti-node-agent
else
    echo "BOOTSTRAP: service installed; waiting for job configuration"
fi
