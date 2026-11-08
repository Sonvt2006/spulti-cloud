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


def agent_settings() -> tuple[str, str, str]:
    base = os.getenv("SPULTI_PUBLIC_API_URL", "").rstrip("/")
    release = os.getenv("SPULTI_AGENT_RELEASE_URL", "")
    digest = os.getenv("SPULTI_AGENT_RELEASE_SHA256", "").lower()
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
            raise ValueError("Managed jobs require HTTPS API and agent release URLs")
    if urlparse(base).query:
        raise ValueError("API URL must not contain a query string")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Set SPULTI_AGENT_RELEASE_SHA256 to the agent archive SHA256")
    return base, release, digest


def startup_script(
    job_id: str, request_id: str, provider: str, user_script: str
) -> str:
    base, release, digest = agent_settings()
    # The URL is an immutable operator-owned release, not an expiring presigned URL.
    # User data is snapshotted, making cloud idempotency retries byte-for-byte stable.
    config = {
        "SPULTI_CONTROL_PLANE_URL": base,
        "SPULTI_AGENT_TOKEN": attempt_token(request_id),
        "SPULTI_JOB_ID": job_id,
    }
    env_text = (
        "\n".join(f"{key}={json.dumps(value)}" for key, value in config.items()) + "\n"
    )

    encoded = base64.b64encode(env_text.encode()).decode()
    installer = INSTALL_SCRIPT.replace(
        "SPULTI_CLOUD_PROVIDER=gcp", f"SPULTI_CLOUD_PROVIDER={provider.lower()}"
    )
    installer = installer.replace(":-gcp}", f":-{provider.lower()}}}")
    start = installer.index("ExecStart=/usr/bin/python3")
    end = installer.index("RestartSec=10", start)
    installer = (
        installer[:start]
        + (
            "ExecStart=/usr/bin/python3 /opt/spulti-agent/agent.py managed "
            "--state-dir /var/lib/spulti-agent\nRestart=on-failure\n"
        )
        + installer[end:]
    )
    return (
        "#!/bin/bash\nset -euo pipefail\nset +x\numask 077\n"
        + (user_script + "\n" if user_script else "")
        + f"printf %s {shlex.quote(encoded)} | base64 -d > /etc/spulti-agent.env\n"
        + f"export SPULTI_AGENT_DOWNLOAD_URL={shlex.quote(release)}\n"
        + f"export SPULTI_AGENT_SHA256={shlex.quote(digest)}\n"
        + installer
    )
