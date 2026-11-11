# Spulti-cloud

## Web dashboard

Giao diện tại `/dashboard/`: nhập nhu cầu để chọn máy Spot phù hợp, quản lý máy,
theo dõi job/checkpoint và so sánh chi phí với On-demand. Xem
[hướng dẫn dashboard](control-plane/DASHBOARD.md) để chạy cùng control-plane trên EC2.

## Chạy trên máy cá nhân

Máy spot có thể bị thu hồi bất cứ lúc nào. Khả năng tiếp tục công việc phụ thuộc vào checkpoint đã lưu thành công và cách chương trình lưu trạng thái; tiến trình trong RAM không tự động được bảo toàn.

## Các thành phần

| Thư mục | Vai trò |
| --- | --- |
| [`cli/`](cli/README.md) | Lệnh `spulti`: tài khoản, API key, SSH key, catalog, placement và quản lý máy |
| [`control-plane/`](control-plane/README.md) | API FastAPI, PostgreSQL, hàng đợi Redis/Celery, chọn máy và điều phối AWS/GCP |
| [`node-agent/`](node-agent/README.md) | Chạy workload trên VM, heartbeat, checkpoint/resume và phát hiện thu hồi |
| `deploy/` | Cấu hình và script triển khai |
| `project-report/` | Tài liệu báo cáo dự án |

Luồng sử dụng: **CLI → Control Plane → máy spot AWS/GCP → Node Agent**. Agent gửi heartbeat và checkpoint về control plane để theo dõi và phục hồi công việc.

## Bắt đầu

### 1. Chuẩn bị control plane

- Phát triển trên máy cá nhân: [hướng dẫn Windows 11 và Ubuntu](control-plane/README.local.md).
- Triển khai server: [hướng dẫn EC2/RDS](control-plane/README.md).
- Lưu checkpoint và phân phối agent trong một bucket S3: [tạo S3, IAM và release Node Agent](control-plane/S3_SETUP.md).
- Chạy job tự động và thay VM khi gián đoạn: [Recovery](control-plane/RECOVERY.md).

Thứ tự deploy cloud: **EC2/RDS → S3 và quyền truy cập → phát hành agent →
HTTPS cho API → cấu hình Instance Manager/catalog → kiểm tra checkpoint → chạy job**.
API healthcheck thành công chưa đủ để xác nhận checkpoint/recovery hoạt động.

Để tạo máy thật, server cần cloud credentials, dữ liệu catalog/giá, cấu hình image/network và worker đang chạy. Xem [Instance Manager](control-plane/INSTANCE_MANAGER.md) để cấu hình đầy đủ.

### 2. Cài CLI

Yêu cầu Python **3.11+**. Tại thư mục gốc repository, trong virtual environment:

```bash
python -m pip install ./cli
spulti --help
```

CLI chỉ dùng thư viện chuẩn Python. Trên Linux, dùng `python3` nếu máy không có lệnh `python`.

### 3. Đăng nhập và đăng ký SSH key

```bash
# Thay URL bằng control plane của bạn; ví dụ local chạy trực tiếp ở cổng 8000.
spulti register --api-url http://localhost:8000 \
  --name "Peter Parker" --username peterparker --email peter@example.com
spulti login --api-url http://localhost:8000 --username peterparker
spulti ssh-key add laptop --file ~/.ssh/id_ed25519.pub
```

Bỏ qua `register` nếu đã có tài khoản. Mật khẩu được nhập qua prompt; `login` lưu API key vào máy để dùng ở các lần sau.

### 4. Chọn và quản lý máy

```bash
spulti placement --vcpu 2 --ram-gib 4 --region singapore
spulti machine create training --vcpu 2 --ram-gib 4 --region singapore
spulti machine list
spulti machine show 1
```

Thay `1` bằng ID trả về khi tạo máy. Yêu cầu tạo/xóa được xử lý bất đồng bộ; xem trạng thái bằng `machine show`. Khi có `instance.public_ip`, kết nối bằng SSH với `ssh_username` trong kết quả. Tạo máy thật sẽ phát sinh chi phí cloud.

```bash
spulti machine events 1
spulti machine delete 1
```

Hướng dẫn đầy đủ, tùy chọn và cách dùng trong CI: [Spulti CLI](cli/README.md).

## Chạy workload và khôi phục

Sau khi SSH vào VM, dùng [`spulti-agent`](node-agent/SSH_JOBS.md) để submit/resume chương trình Python. Lệnh `spulti` trên máy cá nhân quản lý tài nguyên; lệnh `spulti-agent` trên VM chạy workload.

- [Node Agent: checkpoint và demo khôi phục](node-agent/README.md)
- [Chạy chương trình Python qua SSH](node-agent/SSH_JOBS.md)
- [Phát hiện thu hồi AWS/GCP](node-agent/INTERRUPTION.md)
- [Điều phối recovery](control-plane/RECOVERY.md)

## Tài liệu kỹ thuật

| Chủ đề | Tài liệu |
| --- | --- |
| Chọn máy theo giá/rủi ro | [Placement](control-plane/PLACEMENT.md) |
| Thu thập catalog và giá | [Market collector](control-plane/Market_collector.md) |
| S3, quyền checkpoint và phát hành agent | [S3 setup](control-plane/S3_SETUP.md) |
| Vòng đời máy và cấu hình launch | [Instance Manager](control-plane/INSTANCE_MANAGER.md) |
| API tương tác | `/docs` trên control plane đang chạy |
| Quy trình phát triển | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Kiểm tra CLI

Chạy từ thư mục gốc sau khi cài CLI:

```bash
python -m unittest discover -s cli/tests -v
ruff check cli
ruff format --check cli
mypy cli/spulti_cli
```

Bộ test CLI dùng HTTP server tạm trên localhost và không tạo tài nguyên cloud. Các bước kiểm tra toàn repository được mô tả trong [hướng dẫn đóng góp](CONTRIBUTING.md).
