"""CLI quản lý tài khoản và máy trên Spulti-cloud."""

import argparse
import getpass
import json
import math
import os
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from uuid import UUID, uuid4

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
    # Đăng nhập lại trên cùng máy thì thu hồi key cũ để không bị tích tụ.
    old_key, old_key_id = config.get("api_key"), config.get("api_key_id")
    if old_key and old_key_id and config.get("api_url") == api_url:
        try:
            call("DELETE", api_url, f"/api/api-keys/{old_key_id}", token=old_key)
        except APIError as exc:
            if exc.status not in {401, 404}:
                print("Cảnh báo: chưa thu hồi được key cũ.", file=sys.stderr)

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
            config.get("api_url") or DEFAULT_API_URL,
            f"/api/api-keys/{config['api_key_id']}",
            token=config["api_key"],
        )
    except APIError as exc:
        # Key đã bị thu hồi từ nơi khác thì vẫn xóa file cấu hình như bình thường.
        if exc.status not in {401, 404}:
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
        current = (
            " (máy này)"
            if key["id"] == config.get("api_key_id")
            and _api_url(config) == config.get("api_url")
            and _api_key(config) == config.get("api_key")
            else ""
        )
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
    if (
        args.id == config.get("api_key_id")
        and _api_url(config) == config.get("api_url")
        and _api_key(config) == config.get("api_key")
    ):
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
    create.add_argument("--expires-in-days", type=_positive_int)
    create.set_defaults(func=cmd_api_key_create)
    revoke = api_key_commands.add_parser("revoke", help="Thu hồi API key theo ID")
    revoke.add_argument("id", type=_positive_int)
    revoke.set_defaults(func=cmd_api_key_revoke)

    _add_resource_commands(commands)
    return parser


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Giá trị phải lớn hơn 0")
    return number


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("Giá trị không được âm")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Giá trị phải hữu hạn và lớn hơn 0")
    return number


def _limit(value: str) -> int:
    number = _positive_int(value)
    if number > 100:
        raise argparse.ArgumentTypeError("Giá trị tối đa là 100")
    return number


def _print_result(result: Any, as_json: bool) -> None:
    if as_json or not isinstance(result, list):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not result:
        print("Không có kết quả.")
        return
    # Các trường lồng nhau vẫn được giữ đầy đủ bằng --json.
    columns = [
        key
        for key, value in result[0].items()
        if not isinstance(value, dict | list) and key != "public_key"
    ]
    rows = [[str(item.get(key, "-")) for key in columns] for item in result]
    widths = [
        max(len(key), *(len(row[i]) for row in rows)) for i, key in enumerate(columns)
    ]
    print(
        "  ".join(
            key.upper().ljust(width) for key, width in zip(columns, widths, strict=True)
        )
    )
    for row in rows:
        print(
            "  ".join(
                value.ljust(width) for value, width in zip(row, widths, strict=True)
            )
        )


def cmd_resource(args: argparse.Namespace) -> None:
    config = load_config()
    api_url = getattr(args, "api_url", None) or _api_url(config)
    body = None
    headers = None
    path = args.path
    if hasattr(args, "id"):
        path = path.format(id=args.id)
    if args.command == "register":
        if (
            urlparse(api_url).scheme == "http"
            and urlparse(api_url).hostname not in _LOCAL_HOSTS
        ):
            print("Cảnh báo: kết nối HTTP gửi mật khẩu không mã hóa.", file=sys.stderr)
        password = getpass.getpass("Mật khẩu: ")
        if password != getpass.getpass("Nhập lại mật khẩu: "):
            raise ValueError("Mật khẩu xác nhận không khớp")
        body = {
            "name": args.name,
            "username": args.username,
            "email": args.email,
            "password": password,
        }
    elif args.command == "ssh-key" and args.action == "add":
        public_key = Path(args.file).expanduser().read_text(encoding="utf-8").strip()
        if "PRIVATE KEY" in public_key:
            raise ValueError("Chỉ dùng SSH public key (.pub), không dùng private key")
        body = {"name": args.name, "public_key": public_key}
    elif args.command == "placement" or (
        args.command == "machine" and args.action == "create"
    ):
        fields = (
            "vcpu",
            "ram_gib",
            "gpu_count",
            "provider",
            "region",
            "zone",
            "optimization_mode",
            "limit",
        )
        body = {
            key: getattr(args, key) for key in fields if getattr(args, key) is not None
        }
        if args.command == "machine":
            body.update(name=args.name, disk_size_gb=args.disk_size_gb)
            if args.startup_script:
                body["startup_script"] = Path(args.startup_script).read_text(
                    encoding="utf-8"
                )
            if args.job_id:
                body["job_id"] = str(args.job_id)
            request_key = str(args.idempotency_key or uuid4())
            headers = {"Idempotency-Key": request_key}
            print(
                f"Idempotency-Key: {request_key} (dùng lại khi thử lại yêu cầu)",
                file=sys.stderr,
            )
    query = {
        key: getattr(args, key)
        for key in getattr(args, "query_fields", ())
        if getattr(args, key) is not None
    }
    if query:
        path += "?" + urlencode(query)
    result = call(
        args.method,
        api_url,
        path,
        token=_api_key(config) if args.authenticated else None,
        body=body,
        headers=headers,
    )
    _print_result(result, args.json)


def _add_resource_commands(commands: Any) -> None:
    def endpoint(
        parser: argparse.ArgumentParser,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
    ) -> None:
        parser.add_argument("--json", action="store_true", help="In JSON cho script/CI")
        parser.set_defaults(
            func=cmd_resource, method=method, path=path, authenticated=authenticated
        )

    def resources(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
        parser.add_argument("--vcpu", type=_positive_int, required=required)
        parser.add_argument("--ram-gib", type=_positive_float, required=required)
        parser.add_argument("--gpu-count", type=_nonnegative_int, default=0)
        parser.add_argument("--provider", type=str.upper, choices=("AWS", "GCP"))

    def placement_options(parser: argparse.ArgumentParser) -> None:
        resources(parser)
        parser.add_argument("--region")
        parser.add_argument("--zone")
        parser.add_argument(
            "--optimization-mode",
            type=str.upper,
            choices=("SAVING", "STABLE"),
            default="SAVING",
        )
        parser.add_argument("--limit", type=_limit, default=20)

    def pagination(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--limit", type=_limit, default=50)
        parser.add_argument("--offset", type=_nonnegative_int, default=0)
        parser.set_defaults(query_fields=("limit", "offset"))

    register = commands.add_parser("register", help="Tạo tài khoản")
    for field in ("name", "username", "email"):
        register.add_argument(f"--{field}", required=True)
    register.add_argument("--api-url")
    endpoint(register, "POST", "/api/auth/register", authenticated=False)

    health = commands.add_parser("health", help="Kiểm tra API")
    endpoint(health, "GET", "/health", authenticated=False)
    catalog = commands.add_parser("catalog", help="Tra cấu hình máy")
    resources(catalog, required=False)
    catalog.set_defaults(query_fields=("vcpu", "ram_gib", "gpu_count", "provider"))
    endpoint(catalog, "GET", "/api/catalog", authenticated=False)
    placement = commands.add_parser("placement", help="Xếp hạng máy spot phù hợp")
    placement_options(placement)
    endpoint(placement, "POST", "/api/placement/recommendations")

    ssh = commands.add_parser("ssh-key", help="Đăng ký SSH public key")
    ssh_commands = ssh.add_subparsers(dest="action", required=True)
    endpoint(
        ssh_commands.add_parser("list", help="Liệt kê SSH key"), "GET", "/api/ssh-keys"
    )
    add = ssh_commands.add_parser("add", help="Thêm SSH public key từ file .pub")
    add.add_argument("name")
    add.add_argument("--file", required=True)
    endpoint(add, "POST", "/api/ssh-keys")

    machine = commands.add_parser("machine", help="Quản lý vòng đời máy")
    machine_commands = machine.add_subparsers(dest="action", required=True)
    listing = machine_commands.add_parser("list", help="Liệt kê máy")
    pagination(listing)
    endpoint(listing, "GET", "/api/machines")
    create = machine_commands.add_parser("create", help="Yêu cầu tạo máy spot")
    create.add_argument("name")
    placement_options(create)
    create.add_argument("--disk-size-gb", type=_positive_int, default=30)
    create.add_argument("--startup-script", help="Đường dẫn script có shebang")
    create.add_argument("--job-id", type=UUID)
    create.add_argument("--idempotency-key", type=UUID)
    endpoint(create, "POST", "/api/machines")
    for action, method, suffix, help_text in (
        ("show", "GET", "", "Xem trạng thái và IP"),
        ("delete", "DELETE", "", "Yêu cầu xóa máy"),
        ("retry", "POST", "/retry", "Thử lại máy bị chặn"),
        ("instances", "GET", "/instances", "Lịch sử instance"),
        ("events", "GET", "/events", "Lịch sử khôi phục"),
    ):
        sub = machine_commands.add_parser(action, help=help_text)
        sub.add_argument("id", type=_positive_int)
        if action == "events":
            pagination(sub)
        endpoint(sub, method, "/api/machines/{id}" + suffix)

    job = commands.add_parser("job", help="Xem job đã đăng ký bởi node agent")
    job_commands = job.add_subparsers(dest="action", required=True)
    for action, suffix in (
        ("show", ""),
        ("checkpoint", "/checkpoint"),
        ("submission", "/submission"),
    ):
        sub = job_commands.add_parser(action)
        sub.add_argument("id", type=UUID)
        endpoint(sub, "GET", "/api/agent/jobs/{id}" + suffix)


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
    except (OSError, ValueError, EOFError) as exc:
        print(f"Lỗi: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
