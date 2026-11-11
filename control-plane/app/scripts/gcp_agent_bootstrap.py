"""Build a GCP startup script that installs the Spulti Node Agent."""

import re
import shlex

import boto3
from botocore.config import Config

INSTALL_SCRIPT = r"""
set -euo pipefail
umask 077

# Target: official Ubuntu 24.04 image, Python 3.12.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 ca-certificates

if ! id spulti-agent >/dev/null 2>&1; then
    useradd --system --user-group \
        --home-dir /var/lib/spulti-agent \
        --shell /usr/sbin/nologin spulti-agent
fi

install -d -o root -g root -m 0755 /opt/spulti-agent
install -d -o spulti-agent -g spulti-agent -m 0700 \
    /var/lib/spulti-agent

# Do not download again on reboot if this release was installed.
installed_sha="$(cat /opt/spulti-agent/INSTALL_OK 2>/dev/null || true)"

if [ "$installed_sha" != "$SPULTI_AGENT_SHA256" ] ||
   [ ! -f /opt/spulti-agent/agent.py ]; then

    # Avoid replacing files while an old agent is running.
    systemctl stop spulti-node-agent.service 2>/dev/null || true

    python3 - <<'PY'
import hashlib
import os
import shutil
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

url = os.environ["SPULTI_AGENT_DOWNLOAD_URL"]
expected = os.environ["SPULTI_AGENT_SHA256"]

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    archive = root / "agent.tar.gz"
    extracted = root / "extracted"
    extracted.mkdir()

    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                with archive.open("wb") as output:
                    shutil.copyfileobj(response, output)
            break
        except Exception:
            if attempt == 2:
                # Do not print a traceback containing the signed URL.
                raise SystemExit(
                    "Agent download failed; check network or URL expiry"
                ) from None
            time.sleep(5)

    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit("Agent SHA256 mismatch")

    with tarfile.open(archive, "r:gz") as package:
        package.extractall(extracted, filter="data")

    # The release archive must contain agent.py at its root.
    if not (extracted / "agent.py").is_file():
        raise SystemExit("Release archive has no agent.py at its root")

    shutil.copytree(extracted, "/opt/spulti-agent", dirs_exist_ok=True)
PY

    chown -R root:root /opt/spulti-agent
    chmod -R u=rwX,go=rX /opt/spulti-agent

    # Current demo agent uses the Python standard library.
    python3 /opt/spulti-agent/agent.py --help >/dev/null

    printf '%s\n' "$SPULTI_AGENT_SHA256" \
        > /opt/spulti-agent/INSTALL_OK
fi

cat > /usr/local/bin/spulti-agent <<'CLI'
#!/bin/sh
export SPULTI_CLOUD_PROVIDER="${SPULTI_CLOUD_PROVIDER:-gcp}"
exec /usr/bin/python3 /opt/spulti-agent/agent.py "$@"
CLI
chmod 0755 /usr/local/bin/spulti-agent

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
Environment=SPULTI_CLOUD_PROVIDER=gcp
ExecStart=/usr/bin/python3 /opt/spulti-agent/agent.py \
    --job-id ${SPULTI_JOB_ID} \
    --total-steps ${SPULTI_TOTAL_STEPS} \
    --checkpoint-every ${SPULTI_CHECKPOINT_EVERY} \
    --heartbeat-every ${SPULTI_HEARTBEAT_EVERY} \
    --step-delay 5 \
    --state-dir /var/lib/spulti-agent
RestartSec=10
RestartPreventExitStatus=130
TimeoutStopSec=60
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/spulti-agent /workspace/checkpoints

[Install]
WantedBy=multi-user.target
UNIT

chmod 0644 /etc/systemd/system/spulti-node-agent.service
systemctl daemon-reload
systemctl enable spulti-node-agent.service

if [ -f /etc/spulti-agent.env ]; then
    chown root:root /etc/spulti-agent.env
    chmod 0600 /etc/spulti-agent.env
    systemctl start spulti-node-agent.service
    echo "BOOTSTRAP: agent installed; service start requested"
else
    echo "BOOTSTRAP: agent installed; waiting for job configuration"
fi
"""


def build_gcp_agent_startup_script(
    *,
    bucket: str,
    key: str,
    sha256: str,
    region: str = "ap-southeast-1",
    expires_in: int = 3600,
) -> str:
    """Call immediately before creating a new VM.

    This function supplies the installer only.
    Instance Manager supplies /etc/spulti-agent.env.
    """
    digest = sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("SHA256 must contain exactly 64 hexadecimal characters")
    if not bucket or not key:
        raise ValueError("bucket and key are required")
    if not 60 <= expires_in <= 3600:
        raise ValueError("expires_in must be between 60 and 3600 seconds")

    client = boto3.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4"),
    )
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )

    return (
        "#!/bin/bash\n"
        "set +x\n"
        f"export SPULTI_AGENT_DOWNLOAD_URL={shlex.quote(url)}\n"
        f"export SPULTI_AGENT_SHA256={shlex.quote(digest)}\n" + INSTALL_SCRIPT
    )
