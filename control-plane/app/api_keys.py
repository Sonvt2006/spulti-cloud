from datetime import UTC, datetime, timedelta

from app.auth import CurrentUser, DbSession
from app.models import APIKey
from app.security import generate_api_key
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])

# Mỗi lần "spulti login" tạo một key mới, giới hạn để key cũ không tích tụ mãi.
MAX_ACTIVE_API_KEYS = 20


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    # Bỏ trống thì key không hết hạn, chỉ mất hiệu lực khi bị thu hồi.
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class APIKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


class CreatedAPIKeyResponse(APIKeyResponse):
    # Key gốc, chỉ trả về một lần duy nhất khi tạo.
    key: str


@router.post(
    "", status_code=status.HTTP_201_CREATED, response_model=CreatedAPIKeyResponse
)
def create_api_key(
    payload: CreateAPIKeyRequest, user: CurrentUser, db: DbSession
) -> CreatedAPIKeyResponse:
    active_count = db.scalar(
        select(func.count())
        .select_from(APIKey)
        .where(APIKey.user_id == user.id, APIKey.revoked_at.is_(None))
    )
    if active_count is not None and active_count >= MAX_ACTIVE_API_KEYS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Đã có {MAX_ACTIVE_API_KEYS} API key, hãy thu hồi bớt key không dùng",
        )

    key, key_prefix, key_hash = generate_api_key()
    expires_at = None
    if payload.expires_in_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=payload.expires_in_days)

    api_key = APIKey(
        user_id=user.id,
        name=payload.name,
        key_prefix=key_prefix,
        key_hash=key_hash,
        expires_at=expires_at,
    )
    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return CreatedAPIKeyResponse(
        **APIKeyResponse.model_validate(api_key).model_dump(), key=key
    )


@router.get("", response_model=list[APIKeyResponse])
def list_api_keys(user: CurrentUser, db: DbSession) -> list[APIKey]:
    # Chỉ liệt kê key chưa bị thu hồi.
    return list(
        db.scalars(
            select(APIKey)
            .where(APIKey.user_id == user.id, APIKey.revoked_at.is_(None))
            .order_by(APIKey.created_at)
        )
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(key_id: int, user: CurrentUser, db: DbSession) -> Response:
    # Key của người khác cũng trả 404 để không lộ key đó có tồn tại.
    api_key = db.scalars(
        select(APIKey).where(
            APIKey.id == key_id,
            APIKey.user_id == user.id,
            APIKey.revoked_at.is_(None),
        )
    ).first()
    if api_key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Không tìm thấy API key")

    # Giữ lại bản ghi để biết key từng tồn tại, chỉ đánh dấu thời điểm thu hồi.
    api_key.revoked_at = datetime.now(UTC)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
