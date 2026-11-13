"""Stable per-attempt credentials and an unattended, unprivileged job runner."""

import base64
import hashlib
import hmac
import json
import os
import re
import shlex
from urllib.parse import urlparse

from app.scripts.gcp_agent_bootstrap import INSTALL_SCRIPT
from app.security import JWT_SECRET


def attempt_token(request_id: str) -> str:
    return hmac.new(
        JWT_SECRET.encode(), f"managed-agent-v1:{request_id}".encode(), hashlib.sha256
    ).hexdigest()


def node_token(request_id: str) -> str:
    return hmac.new(
        JWT_SECRET.encode(), f"node-agent-v1:{request_id}".encode(), hashlib.sha256
    ).hexdigest()


def agent_settings() -> tuple[str, str, str]:
    base = os.getenv("SPULTI_PUBLIC_API_URL", "").rstrip("/")
    release = os.getenv("SPULTI_AGENT_RELEASE_URL", "")
    digest = os.getenv("SPULTI_AGENT_RELEASE_SHA256", "").lower()
    # Every VM contacts the API, including idle nodes without a job.
    for value in (base, release):
        url = urlparse(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.fragment
            or any(c.isspace() for c in value)
        ):
            raise ValueError("Agent installation requires valid HTTPS URLs")
    if urlparse(base).query:
        raise ValueError("API URL must not contain a query string")

    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Set SPULTI_AGENT_RELEASE_SHA256 to the agent archive SHA256")

    return base, release, digest


def startup_script(
    job_id: str | None,
    request_id: str,
    provider: str,
    user_script: str,
) -> str:
    base, release, digest = agent_settings()

    def write_env(path: str, config: dict[str, str]) -> str:
        env_text = (
            "\n".join(f"{key}={json.dumps(value)}" for key, value in config.items())
            + "\n"
        )
        encoded = base64.b64encode(env_text.encode()).decode()
        return f"printf %s {shlex.quote(encoded)} | base64 -d > {path}\n"

    env_bootstrap = write_env(
        "/etc/spulti-node.env",
        {
            "SPULTI_CONTROL_PLANE_URL": base,
            "SPULTI_NODE_TOKEN": node_token(request_id),
            "SPULTI_NODE_REQUEST_ID": request_id,
        },
    )

    if job_id is not None:
        config = {
            "SPULTI_CONTROL_PLANE_URL": base,
            "SPULTI_AGENT_TOKEN": attempt_token(request_id),
            "SPULTI_JOB_ID": job_id,
        }
        env_bootstrap += write_env("/etc/spulti-agent.env", config)

    return (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "set +x\n"
        "umask 077\n"
        + env_bootstrap
        + f"export SPULTI_AGENT_DOWNLOAD_URL={shlex.quote(release)}\n"
        + f"export SPULTI_AGENT_SHA256={shlex.quote(digest)}\n"
        + installation_script(provider, user_script)
    )


def installation_script(provider: str, user_script: str = "") -> str:
    """Install services using credentials already present on the node."""
    if provider not in {"AWS", "GCP"}:
        raise ValueError("Node provider must be AWS or GCP")
    installer = INSTALL_SCRIPT.replace(
        "SPULTI_CLOUD_PROVIDER=gcp",
        f"SPULTI_CLOUD_PROVIDER={provider.lower()}",
    )

    installer = installer.replace(
        ":-gcp}",
        f":-{provider.lower()}}}",
    )

    # Keep the optional job runner separate from the always-on node monitor.
    installer = installer.replace("spulti-node-agent", "spulti-job-agent")
    installer = installer.replace(
        "systemctl stop spulti-job-agent.service 2>/dev/null || true",
        "systemctl stop spulti-job-agent.service 2>/dev/null || true\n"
        "    systemctl stop spulti-node-agent.service 2>/dev/null || true",
    )
    start = installer.index("ExecStart=/usr/bin/python3")
    end = installer.index("RestartSec=10", start)

    installer = (
        installer[:start]
        + (
            "ExecStart=/usr/bin/python3 "
            "/opt/spulti-agent/agent.py managed "
            "--state-dir /var/lib/spulti-agent\n"
            "Restart=on-failure\n"
        )
        + installer[end:]
    )

    # Start monitoring before user setup (which may be slow or fail), then
    # start the job only after its user-provided dependencies are ready.
    job_start_offset = installer.index("if [ -f /etc/spulti-agent.env ]; then")
    job_start = installer[job_start_offset:]
    installer = installer[:job_start_offset]
    installer += """
if [ ! -f /opt/spulti-agent/node_daemon.py ]; then
    echo "BOOTSTRAP: release lacks node_daemon.py; publish an updated agent" >&2
    exit 1
fi
python3 /opt/spulti-agent/agent.py daemon --help >/dev/null
cat > /etc/systemd/system/spulti-node-agent.service <<'UNIT'
[Unit]
Description=Spulti Node Monitor
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=spulti-agent
Group=spulti-agent
WorkingDirectory=/opt/spulti-agent
EnvironmentFile=/etc/spulti-node.env
Environment=PYTHONUNBUFFERED=1
Environment=SPULTI_CLOUD_PROVIDER=__CLOUD__
ExecStart=/usr/bin/python3 /opt/spulti-agent/agent.py daemon
Restart=always
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/spulti-agent

[Install]
WantedBy=multi-user.target
UNIT
chmod 0600 /etc/spulti-node.env
chmod 0644 /etc/systemd/system/spulti-node-agent.service
systemctl daemon-reload
systemctl enable --now spulti-node-agent.service
echo "BOOTSTRAP: node monitor started"
""".replace("__CLOUD__", provider.lower())

    return (
        installer + ("(\n" + user_script + "\n)\n" if user_script else "") + job_start
    )
