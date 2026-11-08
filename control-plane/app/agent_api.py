"""Control Plane API for demo Node Agents.

Users authenticate with an existing Bearer JWT. Each Node Agent gets a random,
job-scoped token; only the SHA-256 digest is stored in PostgreSQL. No Agent
endpoint permits starting a cloud VM or executing arbitrary shell commands.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from app.auth import CurrentUser
from app.checkpoint_storage import S3CheckpointStore
from app.database import get_db
from app.models import AgentInterruption, AgentJob, AgentProgram
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/agent", tags=["node-agent"])
Db = Annotated[Session, Depends(get_db)]
MAX_CHECKPOINT_BYTES = 16 * 1024


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_steps: int = Field(default=1000, ge=1, le=10_000_000)
    checkpoint_every: int = Field(default=0, ge=0)
    heartbeat_every: int = Field(default=25, ge=1)


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_step: int = Field(ge=0)
    last_checkpoint_step: int = Field(ge=0)
    status: Literal["RUNNING", "INTERRUPTED", "COMPLETED"]


class SumOfSquaresState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    running_sum: int = Field(ge=0, strict=True)


class CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1, 2, 3]
    job_id: str
    total_steps: int
    last_completed_step: int = Field(ge=0)
    status: Literal["RUNNING", "INTERRUPTED", "COMPLETED"]
    updated_at: str | None = None
    workload: Literal["sum_of_squares", "python_script"] | None = None
    workload_state: dict[str, JsonValue] | None = None
    source_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def valid_workload_state(self):
        if self.version == 3:
            if (
                self.workload != "python_script"
                or self.workload_state is None
                or self.source_sha256 is None
            ):
                raise ValueError("Version 3 requires Python script hash and JSON state")
            try:
                encoded = json.dumps(
                    self.workload_state, separators=(",", ":"), allow_nan=False
                )
            except (ValueError, RecursionError) as exc:
                raise ValueError("State must contain finite JSON values") from exc
            if len(encoded.encode("utf-8")) > 8 * 1024:
                raise ValueError("Python script state must not exceed 8 KiB")
            return self
        if self.source_sha256 is not None:
            raise ValueError("Script hash requires checkpoint version 3")
        if self.version == 1:
            if self.workload is not None or self.workload_state is not None:
                raise ValueError("Workload state requires checkpoint version 2")
            return self  # Keep existing counter agents compatible with the API.
        if self.workload != "sum_of_squares" or self.workload_state is None:
            raise ValueError("Version 2 requires sum-of-squares workload state")
        step = self.last_completed_step
        state = SumOfSquaresState.model_validate(self.workload_state)
        if state.running_sum != step * (step + 1) * (2 * step + 1) // 6:
            raise ValueError("Running sum does not match checkpoint step")
        return self


class InterruptionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    provider: Literal["aws", "gcp", "signal"]
    action: Literal[
        "terminate", "stop", "hibernate", "preempt", "sigterm", "sigint", "sighup"
    ]
    detected_at: AwareDatetime
    deadline_at: AwareDatetime | None = None
    current_step: int = Field(ge=0)
    last_checkpoint_step: int = Field(ge=0)
    local_checkpoint_step: int | None = Field(default=None, ge=0)
    checkpoint_status: Literal["uploaded", "failed"]
    checkpoint_error: Literal["local_save_failed", "upload_failed"] | None = None

    @model_validator(mode="after")
    def valid_report(self):
        actions = {
            "aws": {"terminate", "stop", "hibernate"},
            "gcp": {"preempt"},
            "signal": {"sigterm", "sigint", "sighup"},
        }
        if self.action not in actions[self.provider]:
            raise ValueError("Action does not match interruption provider")
        if self.last_checkpoint_step > self.current_step or (
            self.local_checkpoint_step is not None
            and self.local_checkpoint_step > self.current_step
        ):
            raise ValueError("Checkpoint exceeds completed work")
        if self.checkpoint_status == "uploaded" and (
            self.last_checkpoint_step != self.current_step
            or self.local_checkpoint_step != self.current_step
            or self.checkpoint_error is not None
        ):
            raise ValueError("Uploaded checkpoint must match completed work")
        if self.checkpoint_status == "failed" and self.checkpoint_error is None:
            raise ValueError("Failed checkpoint needs an error reason")
        return self


def _job_for_owner(db: Session, job_id: str, user_id: int) -> AgentJob:
    job = db.get(AgentJob, job_id)
    if job is None or job.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


def _job_for_agent(
    db: Session, job_id: str, supplied_token: str | None, *, lock: bool = False
) -> AgentJob:
    # Uniform 401 even if job ID does not exist; no token is ever logged.
    if not supplied_token or not (32 <= len(supplied_token) <= 256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent token")
    job = db.get(
        AgentJob, job_id, with_for_update=True if lock else None, populate_existing=True
    )
    candidate = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
    if job is None or not hmac.compare_digest(candidate, job.agent_token_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent token")
    return job


def _summary(job: AgentJob) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "total_steps": job.total_steps,
        "checkpoint_every": job.checkpoint_every,
        "heartbeat_every": job.heartbeat_every,
        "current_step": job.current_step,
        "checkpoint_step": job.checkpoint_step,
        "last_heartbeat_at": job.last_heartbeat_at,
        "updated_at": job.updated_at,
    }


@router.post("/jobs", status_code=201)
def create_job(payload: CreateJob, user: CurrentUser, db: Db) -> dict:
    token = secrets.token_urlsafe(36)
    job = AgentJob(
        id=str(uuid4()),
        user_id=user.id,
        agent_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        total_steps=payload.total_steps,
        checkpoint_every=payload.checkpoint_every,
        heartbeat_every=payload.heartbeat_every,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {**_summary(job), "agent_token": token}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, user: CurrentUser, db: Db) -> dict:
    job = _job_for_owner(db, job_id, user.id)
    latest = db.scalar(
        select(AgentInterruption)
        .where(AgentInterruption.job_id == job_id)
        .order_by(AgentInterruption.received_at.desc())
        .limit(1)
    )
    return {
        **_summary(job),
        "latest_interruption": (
            {**latest.report, "received_at": latest.received_at} if latest else None
        ),
    }


@router.get("/jobs/{job_id}/config")
def agent_job_config(
    job_id: str, db: Db, x_agent_token: Annotated[str | None, Header()] = None
) -> dict:
    job = _job_for_agent(db, job_id, x_agent_token)
    program = db.get(AgentProgram, job_id)
    return {
        **_summary(job),
        "program": (
            {
                "filename": program.filename,
                "source_code": program.source_code,
                "source_sha256": program.source_sha256,
                "step_delay": float(program.step_delay),
            }
            if program
            else None
        ),
    }


@router.get("/jobs/{job_id}/checkpoint")
def download_checkpoint(
    job_id: str,
    db: Db,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> dict:
    job = _job_for_agent(db, job_id, x_agent_token)

    if job.checkpoint_data is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "No checkpoint yet",
        )

    reference = job.checkpoint_data

    # Giữ khả năng đọc checkpoint cũ trong RDS.
    # Những checkpoint mới sẽ được lưu trên S3.
    if reference.get("storage") != "s3":
        return reference

    try:
        return S3CheckpointStore().load(reference)
    except (BotoCoreError, ClientError, RuntimeError, ValueError):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Checkpoint storage unavailable",
        ) from None


@router.put("/jobs/{job_id}/checkpoint")
def upload_checkpoint(
    job_id: str,
    payload: CheckpointPayload,
    db: Db,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> dict:
    # NO KEY UPDATE still serializes progress writes, while allowing the FK
    # reference from an interruption report even during a slow S3 request.
    job = db.get(AgentJob, job_id, with_for_update={"key_share": True})
    if not x_agent_token or not (32 <= len(x_agent_token) <= 256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent token")
    candidate = hashlib.sha256(x_agent_token.encode()).hexdigest()
    if job is None or not hmac.compare_digest(candidate, job.agent_token_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent token")
    if payload.job_id != job_id or payload.total_steps != job.total_steps:
        raise HTTPException(422, "Checkpoint job_id/total_steps mismatch")
    if payload.version == 3:
        program = db.get(AgentProgram, job_id)
        if program is None or program.source_sha256 != payload.source_sha256:
            raise HTTPException(422, "Checkpoint does not match submitted program")
    step = payload.last_completed_step
    if step > job.total_steps or (
        payload.status == "COMPLETED" and step != job.total_steps
    ):
        raise HTTPException(422, "Invalid checkpoint step/status")
    if step < job.checkpoint_step:
        raise HTTPException(409, "Stale checkpoint cannot overwrite progress")
    if job.status == "COMPLETED" and payload.status != "COMPLETED":
        raise HTTPException(409, "Completed job cannot be downgraded")
    if step == job.checkpoint_step and job.checkpoint_data is not None:
        previous_completed = job.checkpoint_data.get("status") == "COMPLETED"
        if previous_completed and payload.status != "COMPLETED":
            raise HTTPException(409, "Completed checkpoint cannot be downgraded")
    data = payload.model_dump(mode="json", exclude_none=True)
    if (
        len(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        > MAX_CHECKPOINT_BYTES
    ):
        raise HTTPException(413, "Checkpoint too large")
        # S3 phải lưu thành công trước khi cập nhật tiến độ trong RDS.
    try:
        reference = S3CheckpointStore().save(data)
    except (BotoCoreError, ClientError, RuntimeError):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Checkpoint storage unavailable",
        ) from None

    job.checkpoint_data = reference
    job.checkpoint_step = step
    job.current_step = max(job.current_step, step)
    job.status = payload.status
    db.commit()
    return {"status": "saved", "checkpoint_step": step}


@router.post("/jobs/{job_id}/heartbeat")
def post_heartbeat(
    job_id: str,
    payload: Heartbeat,
    db: Db,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> dict:
    job = _job_for_agent(db, job_id, x_agent_token, lock=True)
    if (
        payload.current_step > job.total_steps
        or payload.last_checkpoint_step > payload.current_step
    ):
        raise HTTPException(422, "Invalid heartbeat steps")
    if payload.last_checkpoint_step > job.checkpoint_step:
        raise HTTPException(409, "Checkpoint must be uploaded before heartbeat")
    if job.status == "COMPLETED" and payload.status != "COMPLETED":
        raise HTTPException(409, "Completed job cannot be downgraded")
    if payload.status == "COMPLETED" and (
        job.checkpoint_step != job.total_steps or job.status != "COMPLETED"
    ):
        raise HTTPException(409, "Completed checkpoint must be uploaded first")
    job.current_step = max(job.current_step, payload.current_step)
    job.status = payload.status
    job.last_heartbeat_at = datetime.now(UTC)
    db.commit()
    return {"status": "ack", "checkpoint_step": job.checkpoint_step}


@router.post("/jobs/{job_id}/interruptions")
def report_interruption(
    job_id: str,
    payload: InterruptionPayload,
    db: Db,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> dict:
    job = _job_for_agent(db, job_id, x_agent_token, lock=True)
    event_id = str(payload.event_id)
    data = payload.model_dump(mode="json")

    def acknowledge(existing: AgentInterruption) -> dict:
        if existing.job_id != job_id or existing.report != data:
            raise HTTPException(409, "Interruption event ID already used")
        return {"status": "ack", "event_id": event_id}

    existing = db.get(AgentInterruption, event_id)
    if existing is not None:
        return acknowledge(existing)
    if payload.current_step > job.total_steps:
        raise HTTPException(422, "Interruption exceeds job total steps")
    if payload.last_checkpoint_step > job.checkpoint_step or (
        payload.checkpoint_status == "uploaded" and job.checkpoint_data is None
    ):
        raise HTTPException(409, "Checkpoint has not been acknowledged by platform")

    db.add(AgentInterruption(event_id=event_id, job_id=job_id, report=data))
    try:
        # Persist before touching the job row: a slow S3 upload may hold its lock.
        # Reporting a failed upload itself must never depend on S3 being available.
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(AgentInterruption, event_id)
        if existing is None:
            raise
        return acknowledge(existing)

    job = db.get(
        AgentJob, job_id, with_for_update={"key_share": True}, populate_existing=True
    )
    if not hmac.compare_digest(
        job.agent_token_hash, hashlib.sha256(x_agent_token.encode()).hexdigest()
    ):
        return {"status": "ack", "event_id": event_id}
    # Do not let a delayed report overwrite completion or newer progress.
    if job.status != "COMPLETED" and payload.current_step >= job.current_step:
        job.current_step = payload.current_step
        job.status = "INTERRUPTED"
    db.commit()
    return {"status": "ack", "event_id": event_id}
