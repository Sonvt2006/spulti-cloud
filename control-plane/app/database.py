import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Points to RDS PostgreSQL, e.g.
# postgresql+psycopg://<user>:<password>@<endpoint>.rds.amazonaws.com:5432/spulti?sslmode=require
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://spulti:spulti@localhost:5432/spulti",
)

engine = create_engine(
    DATABASE_URL,
    # Check connections before use, RDS may close idle ones.
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    # FastAPI dependency: one session per request.
    with SessionLocal() as session:
        yield session
