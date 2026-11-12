"""Attempt-scoped node liveness and Spot notices, independent of job storage."""

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from app.database import get_db
from app.machines import _wake_worker
from app.models import InstanceAttempt, MachineLifecycle, NodeAgent
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/agent/nodes", tags=["node-monitor"])
Db = Annotated[Session, Depends(get_db)]


class NodeNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    provider: Literal["aws", "gcp"]
    action: Literal["terminate", "stop", "hibernate", "preempt"]
    detected_at: AwareDatetime
    deadline_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if (self.provider == "gcp") != (self.action == "preempt"):
            raise ValueError("Action does not match interruption provider")
        return self


def _node(db: Session, request_id: UUID, token: str | None):
    if not token or not 32 <= len(token) <= 256:
        raise HTTPException(401, "Invalid node token")
    row = db.execute(
        select(NodeAgent, InstanceAttempt)
        .join(InstanceAttempt, InstanceAttempt.id == NodeAgent.attempt_id)
        .where(InstanceAttempt.request_id == str(request_id))
        .with_for_update(of=NodeAgent)
        .execution_options(populate_existing=True)
    ).first()
    candidate = hashlib.sha256(token.encode()).hexdigest()
    if row is None or not hmac.compare_digest(candidate, row[0].token_hash):
        raise HTTPException(401, "Invalid node token")
    node, attempt = row
    life = db.get(MachineLifecycle, attempt.machine_id, populate_existing=True)
    if (
        node.activated_at is None
        or attempt.state not in {"CREATING", "ACTIVE"}
        or life is None
        or life.delete_requested
    ):
        raise HTTPException(401, "Inactive node token")
    return node, attempt


@router.post("/{request_id}/heartbeat")
def heartbeat(
    request_id: UUID,
    db: Db,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> dict:
    node, _ = _node(db, request_id, x_agent_token)
    node.last_heartbeat_at = datetime.now(UTC)
    db.commit()
    return {"status": "ack"}


@router.post("/{request_id}/interruptions")
def interruption(
    request_id: UUID,
    payload: NodeNotice,
    db: Db,
    x_agent_token: Annotated[str | None, Header()] = None,
) -> dict:
    node, attempt = _node(db, request_id, x_agent_token)
    if payload.provider != attempt.provider.lower():
        raise HTTPException(422, "Notice provider does not match this node")
    report = payload.model_dump(mode="json")
    if node.interruption is not None:
        if node.interruption != report:
            raise HTTPException(409, "This node already reported an interruption")
    else:
        now = datetime.now(UTC)
        node.interruption = report
        node.interruption_received_at = now
        # The job's independent monitor gets its 15-second checkpoint budget
        # before recovery revokes job credentials or deletes the VM.
        node.recovery_after = now + timedelta(seconds=20)
    machine_id = attempt.machine_id
    db.commit()  # Durable notice even if Redis is unavailable.
    life = db.get(MachineLifecycle, machine_id, populate_existing=True)
    life.next_action_at = datetime.now(UTC)
    db.commit()
    _wake_worker(machine_id)
    return {"status": "ack", "event_id": str(payload.event_id)}
