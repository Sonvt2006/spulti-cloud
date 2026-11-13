"""Generate a repair script; run the result as root on the affected node."""

import argparse
import shlex

from app.services.managed_agent import agent_settings, installation_script


def repair_script(provider: str) -> str:
    _, release, digest = agent_settings()
    return (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "set +x\n"
        "umask 077\n"
        'if [ "$(id -u)" -ne 0 ]; then\n'
        '    echo "Run this script with sudo on the affected node" >&2\n'
        "    exit 1\n"
        "fi\n"
        "if [ ! -s /etc/spulti-node.env ]; then\n"
        '    echo "Missing node credentials; use the current managed node" >&2\n'
        "    exit 1\n"
        "fi\n"
        + f"export SPULTI_AGENT_DOWNLOAD_URL={shlex.quote(release)}\n"
        + f"export SPULTI_AGENT_SHA256={shlex.quote(digest)}\n"
        + installation_script(provider)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("AWS", "GCP"), required=True)
    args = parser.parse_args()
    print(repair_script(args.provider), end="")


if __name__ == "__main__":
    main()
