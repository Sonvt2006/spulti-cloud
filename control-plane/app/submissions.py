"""Store user programs and issue job credentials; never execute submitted code."""

import hashlib
import hmac
from uuid import UUID

from app.agent_api import CreateJob, Db, _job_for_owner, _summary
from app.auth import CurrentUser
from app.models import AgentJob, AgentProgram
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from sqlalchemy.exc import IntegrityError

router = APIRouter(prefix="/api/agent", tags=["node-agent"])


class JobCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_token: SecretStr = Field(min_length=32, max_length=256)


class Submission(CreateJob, JobCredential):
    filename: str = Field(max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*\.py$")
    source_code: str = Field(min_length=1, max_length=16384)
    step_delay: float = Field(default=5.0, ge=0, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_program(self):
        if len(self.source_code.encode("utf-8")) > 16384:
            raise ValueError("Program must not exceed 16 KiB")
        try:
            compile(self.source_code, self.filename, "exec")
        except (SyntaxError, ValueError, RecursionError) as exc:
            raise ValueError("Program must be valid Python") from exc
        return self


def _program(db: Db, job_id: str) -> AgentProgram:
    program = db.get(AgentProgram, job_id)
    if program is None:
        raise HTTPException(404, "No submitted program for this job")
    return program


def _submission_summary(job: AgentJob, program: AgentProgram) -> dict:
    return {
        **_summary(job),
        "filename": program.filename,
        "source_code": program.source_code,
        "source_sha256": program.source_sha256,
        "step_delay": float(program.step_delay),
    }


@router.put("/submissions/{job_id}", status_code=201)
def submit_job(job_id: UUID, payload: Submission, user: CurrentUser, db: Db) -> dict:
    """Client-generated UUID + secret allow retries after a lost creation reply."""
    identifier = str(job_id)
    token_hash = hashlib.sha256(
        payload.agent_token.get_secret_value().encode()
    ).hexdigest()
    source_hash = hashlib.sha256(payload.source_code.encode("utf-8")).hexdigest()

    def existing_submission(job: AgentJob) -> dict:
        if job.user_id != user.id:
            raise HTTPException(409, "Submission ID already used")
        program = _program(db, identifier)
        if (
            not hmac.compare_digest(job.agent_token_hash, token_hash)
            or program.source_sha256 != source_hash
            or program.filename != payload.filename
            or round(float(program.step_delay), 3) != round(payload.step_delay, 3)
            or (job.total_steps, job.checkpoint_every, job.heartbeat_every)
            != (payload.total_steps, payload.checkpoint_every, payload.heartbeat_every)
        ):
            raise HTTPException(
                409, "Submission ID already used with different settings"
            )
        return _submission_summary(job, program)

    existing = db.get(AgentJob, identifier)
    if existing is not None:
        return existing_submission(existing)
    job = AgentJob(
        id=identifier,
        user_id=user.id,
        agent_token_hash=token_hash,
        total_steps=payload.total_steps,
        checkpoint_every=payload.checkpoint_every,
        heartbeat_every=payload.heartbeat_every,
    )
    program = AgentProgram(
        job_id=identifier,
        filename=payload.filename,
        source_code=payload.source_code,
        source_sha256=source_hash,
        step_delay=round(payload.step_delay, 3),
    )
    try:
        db.add(job)
        db.flush()
        db.add(program)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(AgentJob, identifier)
        if existing is None:
            raise
        return existing_submission(existing)
    db.refresh(job)
    return _submission_summary(job, program)


@router.get("/jobs/{job_id}/submission")
def get_submission(job_id: UUID, user: CurrentUser, db: Db) -> dict:
    identifier = str(job_id)
    job = _job_for_owner(db, identifier, user.id)
    return _submission_summary(job, _program(db, identifier))


@router.post("/jobs/{job_id}/agent-token")
def replace_agent_token(
    job_id: UUID, payload: JobCredential, user: CurrentUser, db: Db
) -> dict:
    """Owner-authorized resume on a fresh VM; revoke the previous job token."""
    identifier = str(job_id)
    job = db.get(AgentJob, identifier, with_for_update={"key_share": True})
    if job is None or job.user_id != user.id:
        raise HTTPException(404, "Job not found")
    _program(db, identifier)
    job.agent_token_hash = hashlib.sha256(
        payload.agent_token.get_secret_value().encode()
    ).hexdigest()
    db.commit()
    return {"job_id": identifier, "status": "credential_updated"}
