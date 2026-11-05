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
key = "agent/releases/v1/node-agent-v1.tar.gz"
expected = "a0b54fbf805344cbf4d9b24871b9991d4c18122a4a7b7ff9474869bb8126e651"

with tempfile.TemporaryDirectory() as directory:
    archive_path = Path(directory) / "agent.tar.gz"

    boto3.client("s3", region_name="us-east-1").download_file(
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