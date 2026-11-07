"""SSH-user submit/resume commands. Requires only Python's standard library."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

from checkpoint import write_json_atomic
from config import AgentConfig
from control_plane import RemoteError, validate_control_plane_url
from job_runner import run_job
from program import PythonProgram, validate_source


class OwnerAPIError(RemoteError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(
            f"Owner API HTTP {status}; check account access and job settings"
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OwnerClient:
    def __init__(self, url: str, token: str):
        self.url = validate_control_plane_url(url)
        self.token = token
        self.opener = build_opener(_NoRedirect())

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        request = Request(
            self.url + "/api/agent/" + path,
            method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=10) as response:
                    raw = response.read(128 * 1024 + 1)
                    if len(raw) > 128 * 1024:
                        raise RemoteError("Submission response too large")
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise RemoteError("Invalid submission response")
                    return data
            except HTTPError as exc:
                code = exc.code
                exc.close()
                if code not in (429, 500, 502, 503, 504) or attempt == 2:
                    # Validation bodies can echo a secret input. Do not print them.
                    raise OwnerAPIError(code) from None
            except (URLError, TimeoutError, OSError):
                if attempt == 2:
                    raise RemoteError(
                        "Owner API unavailable; retry resume with job ID"
                    ) from None
            time.sleep(0.3 * (2**attempt))
        raise RemoteError("Owner API unavailable")


def owner_client(
    explicit_url: str | None, fallback_url: str | None = None
) -> OwnerClient:
    """Reuse `spulti login` configuration, or an explicitly supplied API key."""
    config_path = Path(
        os.getenv("SPULTI_CONFIG") or str(Path.home() / ".spulti/config.json")
    )
    saved = (
        json.loads(config_path.read_text(encoding="utf-8"))
        if config_path.exists()
        else {}
    )
    url = (
        explicit_url
        or os.getenv("SPULTI_CONTROL_PLANE_URL")
        or os.getenv("SPULTI_API_URL")
        or saved.get("api_url")
        or fallback_url
    )
    if not url:
        raise ValueError("Set --control-plane-url, SPULTI_API_URL, or run spulti login")
    url = validate_control_plane_url(url)
    token = os.getenv("SPULTI_API_KEY")
    if not token and saved.get("api_key"):
        if validate_control_plane_url(saved.get("api_url", "")) != url:
            raise ValueError("Saved API key belongs to another URL; use SPULTI_API_KEY")
        token = saved["api_key"]
    if not token:
        if not sys.stdin.isatty():
            raise ValueError("Set SPULTI_API_KEY or run spulti login on this VM")
        token = getpass.getpass("Spulti API key (or user JWT): ").strip()
    if not token:
        raise ValueError("User API key is required to register/resume a job")
    return OwnerClient(url, token)


@contextmanager
def job_lock(state_dir: Path, job_id: str):
    """One run per job/state directory; the OS releases the lock on process death."""
    directory = state_dir / "jobs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with (directory / f"{job_id}.lock").open("a+b") as lock:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("This job is already running on this VM") from None
        else:
            import fcntl

            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("This job is already running on this VM") from None
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def save_manifest(path: Path, manifest: dict) -> None:
    # write_json_atomic uses NamedTemporaryFile (0600 on POSIX) then os.replace.
    write_json_atomic(path, manifest)
    path.chmod(0o600)


def _check_program(program: dict) -> None:
    source = program["source_code"]
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != program["source_sha256"]:
        raise ValueError("Submitted program checksum mismatch")
    validate_source(source)


def _run(program: dict, token: str, client: OwnerClient, args) -> int:
    _check_program(program)
    config = AgentConfig(
        job_id=program["job_id"],
        total_steps=program["total_steps"],
        checkpoint_every=program["checkpoint_every"],
        heartbeat_every=program["heartbeat_every"],
        step_delay=program["step_delay"],
        state_dir=args.state_dir,
        control_plane_url=client.url,
        agent_token=token,
        cloud_provider=args.cloud_provider,
        checkpoint_seconds=args.checkpoint_seconds,
    )
    print(f"[agent] JOB_ID={config.job_id}", flush=True)
    print(f"[agent] Resume: spulti-agent resume {config.job_id}", flush=True)
    # User code executes with the SSH user's permissions. The root-installed
    # service is not used to execute user submissions.
    return run_job(
        config,
        program=PythonProgram(
            program["source_code"], program["filename"], config.total_steps
        ),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Submit or resume a job from an SSH shell"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="Register a Python program and run it")
    submit.add_argument("file", type=Path)
    submit.add_argument(
        "--total-steps",
        type=int,
        required=True,
        help="Number of complete executions of the Python file",
    )
    submit.add_argument("--step-delay", type=float, default=5.0)
    submit.add_argument("--checkpoint-every", type=int, default=0)
    submit.add_argument("--heartbeat-every", type=int, default=1)
    resume = commands.add_parser(
        "resume", help="Download the program/checkpoint by job ID"
    )
    resume.add_argument("job_id", type=lambda value: str(UUID(value)))
    for command in (submit, resume):
        command.add_argument("--control-plane-url")
        command.add_argument(
            "--checkpoint-seconds",
            type=float,
            default=10.0,
            help="Runtime checkpoint interval in seconds (default: 10)",
        )
        command.add_argument(
            "--state-dir", type=Path, default=Path.home() / ".spulti-agent"
        )
        command.add_argument(
            "--cloud-provider",
            choices=("none", "aws", "gcp"),
            default=os.getenv("SPULTI_CLOUD_PROVIDER", "none").lower(),
        )
    args = parser.parse_args(argv)
    if args.cloud_provider not in {"none", "aws", "gcp"}:
        parser.error("SPULTI_CLOUD_PROVIDER must be none, aws, or gcp")
    if not 0.1 <= args.checkpoint_seconds <= 3600:
        parser.error("checkpoint-seconds must be between 0.1 and 3600 seconds")
    if args.command == "submit":
        if not 1 <= args.total_steps <= 10_000_000:
            parser.error("total-steps must be between 1 and 10000000")
        if args.checkpoint_every < 0 or args.heartbeat_every <= 0:
            parser.error("checkpoint-every must be >= 0 and heartbeat-every > 0")
        if not 0 <= args.step_delay <= 60:
            parser.error("step-delay must be between 0 and 60 seconds")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.py", args.file.name)
            or len(args.file.name) > 128
        ):
            parser.error("Use a .py filename containing letters, digits, dots, - or _")
        source = args.file.read_text(encoding="utf-8")
        validate_source(source)  # Syntax check before creating any remote job.
        job_id = str(uuid4())
    else:
        job_id = args.job_id

    args.state_dir = args.state_dir.expanduser().resolve()
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = args.state_dir / "jobs" / f"{job_id}.json"
    cached_url = None
    if args.command == "resume" and path.exists():
        cached_url = json.loads(path.read_text(encoding="utf-8")).get(
            "control_plane_url"
        )
    client = owner_client(args.control_plane_url, cached_url)
    with job_lock(args.state_dir, job_id):
        if args.command == "submit":
            token = secrets.token_urlsafe(36)
            payload = {
                "filename": args.file.name,
                "source_code": source,
                "total_steps": args.total_steps,
                "checkpoint_every": args.checkpoint_every,
                "heartbeat_every": args.heartbeat_every,
                "step_delay": round(args.step_delay, 3),
                "agent_token": token,
            }
            manifest = {
                "job_id": job_id,
                "control_plane_url": client.url,
                "agent_token": token,
                "pending_submission": payload,
            }
            save_manifest(path, manifest)
            print(
                f"[agent] REGISTERING JOB_ID={job_id}; retry with resume {job_id}",
                flush=True,
            )
            program = client.request("PUT", f"submissions/{job_id}", payload)
        else:
            manifest = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
            if manifest and manifest["control_plane_url"] != client.url:
                raise ValueError("Job state belongs to another Control Plane URL")
            if manifest.get("pending_submission"):
                # The registration reply may have been lost: same ID/token/body.
                program = client.request(
                    "PUT", f"submissions/{job_id}", manifest["pending_submission"]
                )
                token = manifest["agent_token"]
            else:
                program = client.request("GET", f"jobs/{job_id}/submission")
                _check_program(program)
                token = secrets.token_urlsafe(36)
                manifest = {
                    "job_id": job_id,
                    "control_plane_url": client.url,
                    "agent_token": token,
                    "program": program,
                }
                save_manifest(path, manifest)
                client.request(
                    "POST", f"jobs/{job_id}/agent-token", {"agent_token": token}
                )
        _check_program(program)
        save_manifest(
            path,
            {
                "job_id": job_id,
                "control_plane_url": client.url,
                "agent_token": token,
                "program": program,
            },
        )
        return _run(program, token, client, args)
