"""Small demo-agent job metadata and checkpoint in RDS.

Checkpoint JSON is deliberately limited to 16 KiB. Larger workloads should
store checkpoint artifacts in S3/GCS and persist a URI/manifest in the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.database import Base
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_every: Mapped[int] = mapped_column(Integer, nullable=False)
    heartbeat_every: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # JSONB for production Postgres; JSON type on SQLite for isolated tests.
    checkpoint_data: Mapped[dict | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
