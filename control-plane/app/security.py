import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

# Khóa ký JWT, đặt trong .env (xem .env.example).
JWT_SECRET = os.getenv("JWT_SECRET", "")
if len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET phải được đặt và dài ít nhất 32 ký tự")

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

# API key cho CLI có dạng "spk_<chuỗi ngẫu nhiên>", nhờ tiền tố này
# get_current_user phân biệt được API key với JWT.
API_KEY_PREFIX = "spk_"
# Số ký tự đầu được lưu lại để người dùng nhận ra key nào là key nào.
_API_KEY_DISPLAY_LENGTH = 12

_hasher = PasswordHasher()

# Hash giả để kiểm tra khi không tìm thấy user, giúp thời gian phản hồi
# như nhau và không lộ username nào đã tồn tại.
_DUMMY_HASH = _hasher.hash("spulti-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password)
    except VerificationError:
        return False


def create_access_token(user_id: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=JWT_EXPIRE_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> int | None:
    # Trả về user_id, hoặc None nếu token sai chữ ký hay đã hết hạn.
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
        return int(payload["sub"])
    except (jwt.InvalidTokenError, ValueError):
        return None


def generate_api_key() -> tuple[str, str, str]:
    # Trả về (key gốc, phần đầu để hiển thị, mã băm để lưu DB).
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:_API_KEY_DISPLAY_LENGTH], hash_api_key(key)


def hash_api_key(key: str) -> str:
    # Key đủ ngẫu nhiên nên SHA-256 là đủ, không cần argon2 như mật khẩu.
    return hashlib.sha256(key.encode()).hexdigest()
