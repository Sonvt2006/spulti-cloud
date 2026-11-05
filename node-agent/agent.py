"""Run: python agent.py --job-id demo-job-001 --total-steps 1000"""

from __future__ import annotations

import sys

from config import parse_args
from job_runner import run_job


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        return run_job(config)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"[agent] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
