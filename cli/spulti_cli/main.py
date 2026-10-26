"""Lệnh spulti: đăng nhập và quản lý API key."""

import argparse
import getpass
import os
import socket
import sys
from typing import Any
from urllib.parse import urlparse

from spulti_cli.client import (
    CONFIG_PATH,
    DEFAULT_API_URL,
    APIError,
    call,
    delete_config,
    load_config,
    save_config,
)

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _api_url(config: dict[str, Any]) -> str:
    return str(os.getenv("SPULTI_API_URL") or config.get("api_url") or DEFAULT_API_URL)


def _api_key(config: dict[str, Any]) -> str:
    # SPULTI_API_KEY cho phép script hoặc CI dùng key mà không cần file cấu hình.
    key = os.getenv("SPULTI_API_KEY") or config.get("api_key")
    if not key:
        raise APIError(401, "Chưa đăng nhập")
    return str(key)


def cmd_login(args: argparse.Namespace) -> None:
    config = load_config()
    api_url = args.api_url or _api_url(config)
    if urlparse(api_url).scheme == "http" and urlparse(api_url).hostname not in (
        _LOCAL_HOSTS
    ):
        print(
            "Cảnh báo: kết nối không dùng HTTPS, mật khẩu được gửi dưới dạng "
            "không mã hóa.",
            file=sys.stderr,
        )

    username = args.username or input("Username hoặc email: ").strip()
    password = getpass.getpass("Mật khẩu: ")

    # Bước 1: đổi mật khẩu lấy JWT ngắn hạn.
    login = call(
        "POST",
        api_url,
        "/api/auth/login",
        body={"username": username, "password": password},
    )

    # Đăng nhập lại trên cùng máy thì thu hồi key cũ để không bị tích tụ.
    old_key, old_key_id = config.get("api_key"), config.get("api_key_id")
    if old_key and old_key_id and config.get("api_url") == api_url:
        try:
            call("DELETE", api_url, f"/api/api-keys/{old_key_id}", token=old_key)
        except APIError:
            pass  # Key cũ đã bị thu hồi hoặc hết hạn từ trước.

    # Bước 2: dùng JWT tạo API key dài hạn cho CLI.
    key_name = args.key_name or f"{username}-{socket.gethostname()}"[:100]
    created = call(
        "POST",
        api_url,
        "/api/api-keys",
        token=login["access_token"],
        body={"name": key_name},
    )

    # Bước 3: chỉ lưu API key, không lưu mật khẩu hay JWT.
    save_config(
        {"api_url": api_url, "api_key": created["key"], "api_key_id": created["id"]}
    )
    print(f'Đã đăng nhập. API key "{key_name}" đã được lưu vào {CONFIG_PATH}')


def cmd_logout(args: argparse.Namespace) -> None:
    config = load_config()
    if not config.get("api_key") or not config.get("api_key_id"):
        delete_config()
        print("Chưa đăng nhập.")
        return
    try:
        call(
            "DELETE",
            _api_url(config),
            f"/api/api-keys/{config['api_key_id']}",
            token=config["api_key"],
        )
    except APIError as exc:
        # Key đã bị thu hồi từ nơi khác thì vẫn xóa file cấu hình như bình thường.
        if exc.status != 401:
            raise
    delete_config()
    print("Đã đăng xuất và thu hồi API key trên máy này.")


def cmd_whoami(args: argparse.Namespace) -> None:
    config = load_config()
    user = call("GET", _api_url(config), "/api/auth/me", token=_api_key(config))
    print(f"{user['username']} ({user['email']})")


def cmd_api_key_list(args: argparse.Namespace) -> None:
    config = load_config()
    keys = call("GET", _api_url(config), "/api/api-keys", token=_api_key(config))
    if not keys:
        print("Chưa có API key nào.")
        return

    print(f"{'ID':<6}{'TÊN':<32}{'KEY':<16}{'TẠO LÚC':<18}{'DÙNG GẦN NHẤT':<18}")
    for key in keys:
        current = " (máy này)" if key["id"] == config.get("api_key_id") else ""
        print(
            f"{key['id']:<6}{key['name'][:30]:<32}{key['key_prefix'] + '...':<16}"
            f"{_short_time(key['created_at']):<18}"
            f"{_short_time(key['last_used_at']):<18}{current}"
        )


def cmd_api_key_create(args: argparse.Namespace) -> None:
    config = load_config()
    body: dict[str, Any] = {"name": args.name}
    if args.expires_in_days is not None:
        body["expires_in_days"] = args.expires_in_days
    created = call(
        "POST", _api_url(config), "/api/api-keys", token=_api_key(config), body=body
    )
    print(created["key"])
    print("Key chỉ hiện một lần, hãy lưu lại ngay.", file=sys.stderr)


def cmd_api_key_revoke(args: argparse.Namespace) -> None:
    config = load_config()
    call(
        "DELETE",
        _api_url(config),
        f"/api/api-keys/{args.id}",
        token=_api_key(config),
    )
    if args.id == config.get("api_key_id"):
        delete_config()
        print("Đã thu hồi key của máy này. Chạy `spulti login` để đăng nhập lại.")
    else:
        print(f"Đã thu hồi API key {args.id}.")


def _short_time(value: str | None) -> str:
    # "2026-10-26T08:15:30.123+00:00" -> "2026-10-26 08:15"
    return value[:16].replace("T", " ") if value else "-"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spulti", description="CLI của Spulti-cloud")
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="Đăng nhập và lưu API key cho máy này")
    login.add_argument("--api-url", help=f"Địa chỉ control plane ({DEFAULT_API_URL})")
    login.add_argument("--username", help="Username hoặc email")
    login.add_argument("--key-name", help="Tên API key, mặc định <username>-<host>")
    login.set_defaults(func=cmd_login)

    commands.add_parser(
        "logout", help="Thu hồi API key của máy này và đăng xuất"
    ).set_defaults(func=cmd_logout)
    commands.add_parser("whoami", help="Xem tài khoản đang đăng nhập").set_defaults(
        func=cmd_whoami
    )

    api_key = commands.add_parser("api-key", help="Quản lý API key")
    api_key_commands = api_key.add_subparsers(dest="api_key_command", required=True)
    api_key_commands.add_parser("list", help="Liệt kê API key").set_defaults(
        func=cmd_api_key_list
    )
    create = api_key_commands.add_parser(
        "create", help="Tạo API key mới, ví dụ cho script hoặc CI"
    )
    create.add_argument("name")
    create.add_argument("--expires-in-days", type=int)
    create.set_defaults(func=cmd_api_key_create)
    revoke = api_key_commands.add_parser("revoke", help="Thu hồi API key theo ID")
    revoke.add_argument("id", type=int)
    revoke.set_defaults(func=cmd_api_key_revoke)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    try:
        args.func(args)
    except APIError as exc:
        if exc.status == 401 and args.command != "login":
            print(
                f"Lỗi: {exc.message}. Chạy `spulti login` để đăng nhập.",
                file=sys.stderr,
            )
        else:
            print(f"Lỗi: {exc.message}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
