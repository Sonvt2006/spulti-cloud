"""Unattended resume using an attempt-scoped token, never the user's API key."""

import argparse
import hashlib
import os
from pathlib import Path
from uuid import UUID

from config import AgentConfig, add_file_checkpoint_args
from control_plane import ControlPlaneClient
from job_runner import run_job
from program import PythonProgram
from submission import job_lock


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the job assigned to this VM")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/spulti-agent"))
    add_file_checkpoint_args(parser)
    args = parser.parse_args(argv)
    job_id = str(UUID(os.environ["SPULTI_JOB_ID"]))
    client = ControlPlaneClient(
        os.environ["SPULTI_CONTROL_PLANE_URL"], os.environ["SPULTI_AGENT_TOKEN"], job_id
    )
    with job_lock(args.state_dir, job_id):
        data = client.get_config()
        source = data.get("program")
        program = None
        if source:
            if (
                hashlib.sha256(source["source_code"].encode()).hexdigest()
                != source["source_sha256"]
            ):
                raise ValueError("Managed program checksum mismatch")
            program = PythonProgram(
                source["source_code"], source["filename"], data["total_steps"]
            )
        config = AgentConfig(
            job_id=job_id,
            checkpoint_dir=args.checkpoint_dir,
            file_sync_seconds=args.file_sync_seconds,
            total_steps=data["total_steps"],
            checkpoint_every=data["checkpoint_every"],
            heartbeat_every=data["heartbeat_every"],
            step_delay=source["step_delay"] if source else 5.0,
            state_dir=args.state_dir,
            control_plane_url=client.base_url,
            agent_token=client.token,
            cloud_provider=os.environ.get("SPULTI_CLOUD_PROVIDER", "none"),
        )
        return run_job(config, program=program)
