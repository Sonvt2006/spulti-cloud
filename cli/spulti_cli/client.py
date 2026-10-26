"""Gọi REST API của control plane và lưu cấu hình của CLI."""

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
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
        config: dict[str, Any] = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    return config


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # File chứa API key nên chỉ chủ máy được đọc, giống khóa SSH.
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(config, file, indent=2)
    # os.open không đổi quyền nếu file đã tồn tại từ trước.
    CONFIG_PATH.chmod(0o600)


def delete_config() -> None:
    CONFIG_PATH.unlink(missing_ok=True)


def call(
    method: str,
    api_url: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(api_url.rstrip("/") + path, data=data, method=method)
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
    return json.loads(raw) if raw else None


def _error_message(exc: HTTPError) -> str:
    # FastAPI trả {"detail": "..."}, riêng lỗi 422 thì detail là danh sách.
    try:
        detail = json.loads(exc.read())["detail"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {exc.code}"
    if isinstance(detail, list):
        return "; ".join(str(item.get("msg", item)) for item in detail)
    return str(detail)
