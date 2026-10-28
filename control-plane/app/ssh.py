"""Quá trình xác thực được chia sẻ giữa việc đăng ký khóa SSH và cấp phát phiên bản."""

import base64
import hashlib

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

MAX_PUBLIC_KEY_LENGTH = 8192


def normalize_public_key(value: str) -> str:
    """Chấp nhận một khóa công khai OpenSSH duy nhất và chuẩn hóa."""

    value = value.strip()
    if (
        not value
        or len(value) > MAX_PUBLIC_KEY_LENGTH
        or any(char in value for char in ("\n", "\r", "\x00"))
    ):
        raise ValueError(
            f"Cần một public key OpenSSH hợp lệ, dài không quá {MAX_PUBLIC_KEY_LENGTH}"
        )

    try:
        key = serialization.load_ssh_public_key(value.encode("utf-8"))
    except (ValueError, UnsupportedAlgorithm) as exc:
        raise ValueError("Public key OpenSSH không hợp lệ") from exc

    if not isinstance(
        key, rsa.RSAPublicKey | ed25519.Ed25519PublicKey | ec.EllipticCurvePublicKey
    ):
        raise ValueError("Chỉ hỗ trợ SSH key RSA, Ed25519 hoặc ECDSA")

    if isinstance(key, rsa.RSAPublicKey) and key.key_size < 2048:
        raise ValueError("RSA key phải có ít nhất 2048 bit")

    return key.public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode("ascii")


def public_key_fingerprint(normalized_key: str) -> str:
    """Tạo fingerprint SHA256 của public key OpenSSH đã chuẩn hóa."""

    blob = base64.b64decode(normalized_key.split()[1], validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")
