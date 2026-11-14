"""Run a Python workload locally with the same epoch/step runner used on VMs."""

import argparse
import re
from pathlib import Path

from config import AgentConfig
from job_runner import run_job
from program import PythonProgram
from submission import job_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--total-steps", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    args = parser.parse_args(argv)
    if not 1 <= args.total_steps <= 10_000_000:
        parser.error("total-steps must be between 1 and 10000000")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", args.job_id):
        parser.error("Invalid job ID")
    root = args.state_dir.expanduser().resolve()
    program = PythonProgram(
        args.file.read_text(encoding="utf-8"), args.file.name, args.total_steps
    )
    config = AgentConfig(
        job_id=args.job_id,
        total_steps=args.total_steps,
        checkpoint_every=1,
        heartbeat_every=1,
        step_delay=0,
        state_dir=root,
        checkpoint_dir=root / "files",
    )
    with job_lock(root, args.job_id):
        return run_job(config, program=program)
