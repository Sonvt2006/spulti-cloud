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
from uuid import uuid4

from app.auth import CurrentUser
from app.database import get_db
from app.models import AgentJob
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/agent", tags=["node-agent"])
Db = Annotated[Session, Depends(get_db)]
MAX_CHECKPOINT_BYTES = 16 * 1024


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_steps: int = Field(default=1000, ge=1, le=10_000_000)
    checkpoint_every: int = Field(default=100, ge=1)
    heartbeat_every: int = Field(default=25, ge=1)


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_step: int = Field(ge=0)
    last_checkpoint_step: int = Field(ge=0)
    status: Literal["RUNNING", "INTERRUPTED", "COMPLETED"]


class CheckpointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    job_id: str
    total_steps: int
    last_completed_step: int = Field(ge=0)
    status: Literal["RUNNING", "INTERRUPTED", "COMPLETED"]
    updated_at: str | None = None


def _job_for_owner(db: Session, job_id: str, user_id: int) -> AgentJob:
    job = db.get(AgentJob, job_id)
    if job is None or job.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


def _job_for_agent(db: Session, job_id: str, supplied_token: str | None) -> AgentJob:
    # Uniform 401 even if job ID does not exist; no token is ever logged.
    if not supplied_token or not (32 <= len(supplied_token) <= 256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent token")
    job = db.get(AgentJob, job_id)
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
    return _summary(_job_for_owner(db, job_id, user.id))


@router.get("/jobs/{job_id}/config")
def agent_job_config(
    job_id: str, db: Db, x_agent_token: Annotated[str | None, Header()] = None
) -> dict:
    return _summary(_job_for_agent(db, job_id, x_agent_token))


@router.get("/jobs/{job_id}/checkpoint")
def download_checkpoint(
    job_id: str, db: Db, x_agent_token: Annotated[str | None, Header()] = None
) -> dict:
    job = _job_for_agent(db, job_id, x_agent_token)
    if job.checkpoint_data is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No checkpoint yet")
    return job.checkpoint_data


@router.put("/jobs/{job_id}/checkpoint")
def upload_checkpoint(
    job_id: str,
    payload: CheckpointPayload,
    db: Db,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> dict:
    # Row lock prevents two concurrent workers overwriting a newer checkpoint.
    job = db.get(AgentJob, job_id, with_for_update=True)
    if not x_agent_token or not (32 <= len(x_agent_token) <= 256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent token")
    candidate = hashlib.sha256(x_agent_token.encode()).hexdigest()
    if job is None or not hmac.compare_digest(candidate, job.agent_token_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid agent token")
    if payload.job_id != job_id or payload.total_steps != job.total_steps:
        raise HTTPException(422, "Checkpoint job_id/total_steps mismatch")
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
    job.checkpoint_data = data
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
    job = _job_for_agent(db, job_id, x_agent_token)
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
