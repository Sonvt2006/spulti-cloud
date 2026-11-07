"""Run: python agent.py --job-id demo-job-001 --total-steps 1000"""

from __future__ import annotations

import sys

from config import parse_args
from job_runner import run_job


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] in {"submit", "resume"}:
            from submission import main as submission_main

            return submission_main(argv)
        config = parse_args(argv)
        return run_job(config)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"[agent] ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
