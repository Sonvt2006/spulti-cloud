"""Isolated HTTP fixture: real new agent endpoints + SQLAlchemy + auth stub.

Production uses the team's app.auth JWT; here we stub that dependency so
unit/e2e tests do not need Docker, Postgres or cloud secrets.
"""

import os
import sys
import types
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))

from app.database import SessionLocal, engine  # noqa: E402
from app.models import AgentInterruption, AgentJob, AgentProgram, User  # noqa: E402


def get_current_user(authorization: str | None = Header(default=None)):
    identities = {"Bearer test-jwt-user1": 1, "Bearer test-jwt-user2": 2}
    if authorization not in identities:
        raise HTTPException(401, "Requires owner JWT")
    return types.SimpleNamespace(id=identities[authorization], username="tester")


mod_auth = types.ModuleType("app.auth")
mod_auth.CurrentUser = Annotated[User, Depends(get_current_user)]
sys.modules["app.auth"] = mod_auth

from app import agent_api  # noqa: E402
from app.agent_api import router as agent_router  # noqa: E402
from app.submissions import router as submission_router  # noqa: E402


class MemoryCheckpointStore:
    """Keep the real HTTP/DB flow isolated from S3 and developer credentials."""

    objects = {}
    fail_interrupted = False

    def save(self, payload):
        if self.fail_interrupted and payload["status"] == "INTERRUPTED":
            raise RuntimeError("Simulated checkpoint storage outage")
        from uuid import uuid4

        key = str(uuid4())
        self.objects[key] = payload.copy()
        return {"storage": "s3", "key": key, "status": payload["status"]}

    def load(self, reference):
        return self.objects[reference["key"]].copy()


agent_api.S3CheckpointStore = MemoryCheckpointStore

app = FastAPI()
app.include_router(agent_router)
app.include_router(submission_router)


@app.post("/_test/fail-interrupted-checkpoint")
def fail_interrupted_checkpoint():
    MemoryCheckpointStore.fail_interrupted = True
    return {"status": "ok"}


if __name__ == "__main__":
    User.__table__.create(engine, checkfirst=True)
    AgentJob.__table__.create(engine, checkfirst=True)
    AgentInterruption.__table__.create(engine, checkfirst=True)
    AgentProgram.__table__.create(engine, checkfirst=True)
    with SessionLocal.begin() as db:
        if db.get(User, 1) is None:
            db.add(
                User(
                    id=1,
                    name="Tester",
                    email="tester@example.test",
                    username="tester",
                    password_hash="test-only",
                )
            )
        if db.get(User, 2) is None:
            db.add(
                User(
                    id=2,
                    name="Other",
                    email="other@example.test",
                    username="other",
                    password_hash="test-only",
                )
            )
    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ["TEST_PORT"]), log_level="warning"
    )
