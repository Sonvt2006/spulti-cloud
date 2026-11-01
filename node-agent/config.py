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


def parse_args(argv: list[str] | None = None) -> AgentConfig:
    parser = argparse.ArgumentParser(
        description="Spulti Node Agent MVP (demo workload)"
    )
    parser.add_argument("--job-id", default="demo-job-001")
    parser.add_argument("--total-steps", type=int, default=1000)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    parser.add_argument("--heartbeat-every", type=int, default=25)
    parser.add_argument(
        "--step-delay", type=float, default=0.01, help="Seconds per simulated step"
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
    args = parser.parse_args(argv)
    token = os.getenv("SPULTI_AGENT_TOKEN")

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.job_id):
        parser.error(
            "job-id: 1..64 letters/numbers/dot/underscore/hyphen, starting alphanumeric"
        )
    if args.total_steps <= 0 or args.checkpoint_every <= 0 or args.heartbeat_every <= 0:
        parser.error("total-steps, checkpoint-every, heartbeat-every must be positive")
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
