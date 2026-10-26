import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# Địa chỉ RDS PostgreSQL, ví dụ:
# postgresql+psycopg://<user>:<password>@<endpoint>.rds.amazonaws.com:5432/spulti?sslmode=require
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://spulti:spulti@localhost:5432/spulti",
)

engine = create_engine(
    DATABASE_URL,
    # Kiểm tra kết nối trước khi dùng, vì RDS có thể ngắt kết nối để lâu không dùng.
    pool_pre_ping=True,
    pool_recycle=1800,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    # Dependency của FastAPI: mỗi request dùng một session riêng.
    with SessionLocal() as session:
        yield session
