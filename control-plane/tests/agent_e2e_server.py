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
from app.models import AgentJob, User  # noqa: E402


def get_current_user(authorization: str | None = Header(default=None)):
    if authorization != "Bearer test-jwt-user1":
        raise HTTPException(401, "Requires owner JWT")
    return types.SimpleNamespace(id=1, username="tester")


mod_auth = types.ModuleType("app.auth")
mod_auth.CurrentUser = Annotated[User, Depends(get_current_user)]
sys.modules["app.auth"] = mod_auth

from app.agent_api import router as agent_router  # noqa: E402

app = FastAPI()
app.include_router(agent_router)

if __name__ == "__main__":
    User.__table__.create(engine, checkfirst=True)
    AgentJob.__table__.create(engine, checkfirst=True)
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
    uvicorn.run(
        app, host="127.0.0.1", port=int(os.environ["TEST_PORT"]), log_level="warning"
    )
