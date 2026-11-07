"""CLI parsing and validation for the standalone demonstration workload."""

from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    job_id: str
    total_steps: int
    checkpoint_every: int
    heartbeat_every: int
    step_delay: float
    state_dir: Path
    simulate_crash_at: int | None = None
    control_plane_url: str | None = None
    agent_token: str | None = None
    cloud_provider: str = "none"
    interruption_poll_interval: float = 2.0
    shutdown_timeout: float = 15.0
    checkpoint_seconds: float = 10.0


def parse_args(argv: list[str] | None = None) -> AgentConfig:
    parser = argparse.ArgumentParser(
        description="Spulti Node Agent: sum of squares with checkpoint/restore",
        epilog="SSH: agent.py submit FILE.py --total-steps N | agent.py resume JOB_ID",
    )
    parser.add_argument("--job-id", default="demo-job-001")
    parser.add_argument("--total-steps", type=int, default=1000)
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=0,
        help="N > 0: also save every N completed steps; 0 disables this step trigger",
    )
    parser.add_argument(
        "--checkpoint-seconds",
        type=float,
        default=10.0,
        help="Save the latest completed step/state at this time interval (default: 10)",
    )
    parser.add_argument("--heartbeat-every", type=int, default=25)
    parser.add_argument(
        "--step-delay", type=float, default=5.0, help="Seconds to wait before each step"
    )
    parser.add_argument("--state-dir", type=Path, default=Path("./state"))
    parser.add_argument(
        "--simulate-crash-at",
        type=int,
        help="TEST ONLY: simulate abrupt process loss after this step",
    )
    parser.add_argument(
        "--control-plane-url",
        default=os.getenv("SPULTI_CONTROL_PLANE_URL"),
        help="Control Plane URL, e.g. http://localhost:8080 or HTTPS URL",
    )
    # Tokens are intentionally never accepted in CLI args (visible in ps output).
    parser.add_argument(
        "--cloud-provider",
        choices=("none", "aws", "gcp"),
        default=os.getenv("SPULTI_CLOUD_PROVIDER", "none").lower(),
        help="Spot metadata provider; none disables metadata polling (local runs)",
    )
    parser.add_argument("--interruption-poll-interval", type=float, default=2.0)
    parser.add_argument("--shutdown-timeout", type=float, default=15.0)
    args = parser.parse_args(argv)
    token = os.getenv("SPULTI_AGENT_TOKEN")

    if args.cloud_provider not in {"none", "aws", "gcp"}:
        parser.error("SPULTI_CLOUD_PROVIDER must be none, aws, or gcp")
    if not 0.1 <= args.interruption_poll_interval <= 5:
        parser.error("interruption-poll-interval must be between 0.1 and 5 seconds")
    if not 3 <= args.shutdown_timeout <= 120:
        parser.error("shutdown-timeout must be between 3 and 120 seconds")

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.job_id):
        parser.error(
            "job-id: 1..64 letters/numbers/dot/underscore/hyphen, starting alphanumeric"
        )
    if args.total_steps <= 0 or args.heartbeat_every <= 0:
        parser.error("total-steps and heartbeat-every must be positive")
    if args.checkpoint_every < 0:
        parser.error("checkpoint-every must be zero or positive")
    if not 0.1 <= args.checkpoint_seconds <= 3600:
        parser.error("checkpoint-seconds must be between 0.1 and 3600 seconds")
    if not 0 <= args.step_delay <= 60:
        parser.error("step-delay must be between 0 and 60 seconds")
    if (
        args.simulate_crash_at is not None
        and not 1 <= args.simulate_crash_at <= args.total_steps
    ):
        parser.error("simulate-crash-at must be within 1..total-steps")
    if bool(args.control_plane_url) != bool(token):
        parser.error(
            "Remote mode needs BOTH --control-plane-url and SPULTI_AGENT_TOKEN"
        )
    return AgentConfig(**vars(args), agent_token=token)
