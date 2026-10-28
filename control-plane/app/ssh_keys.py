from datetime import datetime

from app.auth import CurrentUser, DbSession
from app.models import SSHKey
from app.ssh import MAX_PUBLIC_KEY_LENGTH, normalize_public_key, public_key_fingerprint
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

router = APIRouter(prefix="/api/ssh-keys", tags=["ssh-keys"])


class CreateSSHKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=1, max_length=MAX_PUBLIC_KEY_LENGTH)

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        return normalize_public_key(value)


class SSHKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    public_key: str
    fingerprint: str
    created_at: datetime


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SSHKeyResponse)
def create_ssh_key(
    payload: CreateSSHKeyRequest, user: CurrentUser, db: DbSession
) -> SSHKey:
    key = SSHKey(
        user_id=user.id,
        name=payload.name,
        public_key=payload.public_key,
        fingerprint=public_key_fingerprint(payload.public_key),
    )

    db.add(key)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Tên hoặc public key này đã được đăng ký"
        ) from exc

    db.refresh(key)
    return key


@router.get("", response_model=list[SSHKeyResponse])
def list_ssh_keys(user: CurrentUser, db: DbSession) -> list[SSHKey]:
    return list(
        db.scalars(select(SSHKey).where(SSHKey.user_id == user.id).order_by(SSHKey.id))
    )
