"""Gọi REST API của control plane và lưu cấu hình của CLI."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CONFIG_PATH = Path(
    os.getenv("SPULTI_CONFIG", str(Path.home() / ".spulti" / "config.json"))
)
DEFAULT_API_URL = "http://localhost"


class APIError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        # 0 nghĩa là không kết nối được tới server.
        self.status = status
        self.message = message


def load_config() -> dict[str, Any]:
    try:
        config: dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(config, dict):
        raise ValueError("Cấu hình CLI phải là một JSON object")
    for key in ("api_url", "api_key"):
        if key in config and not isinstance(config[key], str):
            raise ValueError(f"Trường {key} trong cấu hình phải là chuỗi")
    if "api_key_id" in config and type(config["api_key_id"]) is not int:
        raise ValueError("Trường api_key_id trong cấu hình phải là số nguyên")
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Ghi nguyên tử, giữ bản cũ nếu ghi lỗi; file tạm chỉ chủ máy được đọc.
    fd, name = tempfile.mkstemp(dir=CONFIG_PATH.parent, prefix=".config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(config, file, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, CONFIG_PATH)
    finally:
        Path(name).unlink(missing_ok=True)


def delete_config() -> None:
    CONFIG_PATH.unlink(missing_ok=True)


def call(
    method: str,
    api_url: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    parsed = urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("API URL phải là địa chỉ http:// hoặc https:// hợp lệ")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API URL không được chứa credentials, query hoặc fragment")
    data = json.dumps(body).encode() if body is not None else None
    request = Request(api_url.rstrip("/") + path, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")

    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read()
    except HTTPError as exc:
        raise APIError(exc.code, _error_message(exc)) from exc
    except URLError as exc:
        raise APIError(0, f"Không kết nối được tới {api_url}: {exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise APIError(0, f"Không kết nối được tới {api_url}: {exc}") from exc
    try:
        return json.loads(raw) if raw else None
    except ValueError as exc:
        raise APIError(0, "Server trả về dữ liệu không phải JSON") from exc


def _error_message(exc: HTTPError) -> str:
    # FastAPI trả {"detail": "..."}, riêng lỗi 422 thì detail là danh sách.
    try:
        detail = json.loads(exc.read())["detail"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {exc.code}"
    if isinstance(detail, list):
        return "; ".join(
            str(item.get("msg", item) if isinstance(item, dict) else item)
            for item in detail
        )
    return str(detail)
