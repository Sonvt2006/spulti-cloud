from datetime import UTC, datetime, timedelta
from typing import Annotated

from app.database import get_db
from app.models import APIKey, User
from app.security import (
    API_KEY_PREFIX,
    JWT_EXPIRE_MINUTES,
    create_access_token,
    decode_access_token,
    hash_api_key,
    hash_password,
    verify_password,
)
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/auth", tags=["auth"])

DbSession = Annotated[Session, Depends(get_db)]


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    email: EmailStr
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email", "username")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        # Lưu chữ thường để "Son" và "son" không thành hai tài khoản.
        return value.lower()


class LoginRequest(BaseModel):
    # Nhận username hoặc email.
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    email: str
    username: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Chưa đăng nhập hoặc token không hợp lệ",
    headers={"WWW-Authenticate": "Bearer"},
)

# Chỉ cập nhật last_used_at mỗi phút một lần để không phải ghi DB ở mọi request.
_LAST_USED_INTERVAL = timedelta(minutes=1)


def _user_from_api_key(db: Session, key: str) -> User | None:
    api_key = db.scalars(
        select(APIKey).where(APIKey.key_hash == hash_api_key(key))
    ).first()
    now = datetime.now(UTC)
    if api_key is None or api_key.revoked_at is not None:
        return None
    if api_key.expires_at is not None and api_key.expires_at <= now:
        return None

    if api_key.last_used_at is None or now - api_key.last_used_at > _LAST_USED_INTERVAL:
        api_key.last_used_at = now
        db.commit()
    return api_key.user


def get_current_user(
    db: DbSession,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
    ],
) -> User:
    # Dependency cho mọi endpoint cần đăng nhập.
    # Nhận JWT (Dashboard) hoặc API key "spk_..." (CLI) trong cùng header.
    if credentials is None:
        raise _UNAUTHORIZED

    token = credentials.credentials
    user: User | None
    if token.startswith(API_KEY_PREFIX):
        user = _user_from_api_key(db, token)
    else:
        user_id = decode_access_token(token)
        user = db.get(User, user_id) if user_id is not None else None

    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post(
    "/register", status_code=status.HTTP_201_CREATED, response_model=UserResponse
)
def register(payload: RegisterRequest, db: DbSession) -> User:
    taken = db.scalars(
        select(User).where(
            or_(User.email == payload.email, User.username == payload.username)
        )
    ).first()
    if taken is not None:
        field = "Email" if taken.email == payload.email else "Username"
        raise HTTPException(status.HTTP_409_CONFLICT, f"{field} đã được sử dụng")

    user = User(
        name=payload.name,
        email=payload.email,
        username=payload.username,
        password_hash=hash_password(payload.password),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        # Hai request đăng ký cùng lúc với cùng email/username.
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Email hoặc username đã được sử dụng"
        ) from exc
    return user


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    identifier = payload.username.lower()
    user = db.scalars(
        select(User).where(or_(User.username == identifier, User.email == identifier))
    ).first()

    # Luôn gọi verify_password để không lộ username nào tồn tại.
    password_ok = verify_password(
        payload.password, user.password_hash if user else None
    )
    if user is None or not password_ok or not user.is_active:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Sai username/email hoặc mật khẩu"
        )

    return TokenResponse(
        access_token=create_access_token(user.id),
        expires_in=JWT_EXPIRE_MINUTES * 60,
    )


@router.get("/me", response_model=UserResponse)
def me(user: CurrentUser) -> User:
    return user
