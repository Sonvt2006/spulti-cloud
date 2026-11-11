# Spulti CLI

Lệnh `spulti` làm việc với control plane từ máy cá nhân hoặc CI: quản lý tài khoản, API key, SSH key, tìm cấu hình, chọn máy spot và quản lý vòng đời máy. Yêu cầu Python **3.11+**, không có dependency runtime ngoài thư viện chuẩn.

## Cài đặt

Từ thư mục gốc repository, trong virtual environment:

```bash
python -m pip install ./cli
spulti --help
spulti machine create --help
```

Dùng `python -m pip install -e ./cli` khi phát triển. Có thể gọi `python -m spulti_cli` thay cho `spulti`.

## Tài khoản và API key

```bash
spulti register --api-url http://localhost:8000 \
  --name "Peter Parker" --username peterparker --email peter@example.com
spulti login --api-url http://localhost:8000 --username peterparker
spulti whoami
spulti api-key list
spulti api-key create ci --expires-in-days 30
spulti api-key revoke 2
spulti logout
```

`register` chỉ tạo tài khoản, không đăng nhập hoặc lưu URL. `login` hỏi mật khẩu, lấy JWT ngắn hạn rồi đổi thành API key; chỉ API key và URL được lưu vào `~/.spulti/config.json`. File được thay thế nguyên tử và có quyền `600` trên POSIX. Mật khẩu và JWT không được lưu.

Tên key mặc định là `<username>-<tên máy>`; đổi bằng `login --key-name`. Đăng nhập lại cùng server sẽ lưu key mới trước khi thu hồi key cũ. Nếu thu hồi lỗi, CLI cảnh báo để bạn kiểm tra bằng `api-key list`. API key tạo cho CI chỉ được in một lần; lưu lại trước khi đóng terminal.

`logout` thu hồi key trên **server đã lưu trong cấu hình**, sau đó xóa cấu hình. Nếu mất kết nối, cấu hình được giữ lại để thử lại. Lệnh này không thu hồi key truyền qua biến môi trường. Kết nối HTTP ngoài localhost có cảnh báo vì mật khẩu không được mã hóa; dùng URL HTTPS nếu server hỗ trợ.

## SSH key, catalog và placement

```bash
spulti ssh-key add laptop --file ~/.ssh/id_ed25519.pub
spulti ssh-key list
spulti catalog --vcpu 2 --ram-gib 4 --provider AWS
spulti placement --vcpu 2 --ram-gib 4 --region singapore --optimization-mode BALANCED
```

Chỉ gửi SSH **public key** (`.pub`). API hiện hỗ trợ thêm và liệt kê SSH key. Đăng ký ít nhất một key trước khi tạo máy.

`catalog` liệt kê cấu hình, còn `placement` xếp hạng ứng viên spot theo giá/rủi ro. Placement không tạo máy hoặc giữ chỗ tài nguyên. Các bộ lọc gồm `--provider AWS|GCP`, `--region`, `--zone`, `--gpu-count`; chế độ tối ưu gồm `SAVING`, `STABLE`, `BALANCED`. `--region singapore` là nhóm khu vực chung AWS/GCP. Xem [Placement](../control-plane/PLACEMENT.md) về ý nghĩa điểm xếp hạng.

## Quản lý máy

```bash
spulti machine create training --vcpu 2 --ram-gib 4 \
  --region singapore --disk-size-gb 30
spulti machine list --limit 20 --offset 0
spulti machine show 1
spulti machine instances 1
spulti machine events 1 --limit 20 --offset 0
spulti machine retry 1
spulti machine delete 1
```

Thay `1` bằng ID của máy. `create` hỗ trợ bộ lọc như placement, `--startup-script FILE` (script bắt đầu bằng shebang), và `--job-id UUID` để gắn job đã submit. Cấu hình image/network và cloud credentials nằm ở server; xem [Instance Manager](../control-plane/INSTANCE_MANAGER.md).

Tạo/xóa máy trả về trạng thái hiện tại ngay khi API nhận yêu cầu; worker xử lý tiếp ở nền. Theo dõi bằng `machine show`. `retry` yêu cầu thử lại máy bị chặn, `instances` xem lịch sử VM, `events` xem lịch sử recovery. Khi máy có IP, dùng `ssh_username` và `instance.public_ip` trong kết quả để SSH. Tạo máy thật phát sinh chi phí cloud; lệnh `delete` gửi yêu cầu xóa ngay.

Mỗi lần `create` in `Idempotency-Key` ra stderr trước khi gửi request. Nếu mất phản hồi, thử lại với **cùng key và cùng tham số** để tránh tạo trùng:

```bash
spulti machine create training --vcpu 2 --ram-gib 4 --region singapore \
  --idempotency-key 32b0c7c5-eceb-4cff-a391-60ebd94dc90f
```

## Xem job

```bash
spulti job show JOB_UUID
spulti job checkpoint JOB_UUID
spulti job submission JOB_UUID
```

Thay `JOB_UUID` bằng UUID thực. Các lệnh này đọc trạng thái, checkpoint và chương trình đã submit của tài khoản. Để submit/resume chương trình trên VM, dùng [`spulti-agent`](../node-agent/SSH_JOBS.md).

## Script, cấu hình và lỗi

Các lệnh `register`, `health`, `catalog`, `placement`, `ssh-key`, `machine` và `job` có `--json`. Danh sách mặc định hiển thị dạng bảng; kết quả chi tiết dạng JSON. `--json` giữ toàn bộ trường và trả `[]` cho danh sách rỗng.

```bash
spulti health
spulti machine list --json > machines.json
spulti placement --vcpu 2 --ram-gib 4 --json > candidates.json
```

| Biến | Ý nghĩa |
| --- | --- |
| `SPULTI_API_URL` | URL control plane, ưu tiên hơn cấu hình đã lưu |
| `SPULTI_API_KEY` | Key dùng thay key đã lưu, phù hợp cho CI |
| `SPULTI_CONFIG` | Đường dẫn cấu hình, mặc định `~/.spulti/config.json` |

Với `login` và `register`, `--api-url` ưu tiên cao nhất. URL mặc định khi chưa cấu hình là `http://localhost` (cổng 80); API local chạy trực tiếp thường cần `http://localhost:8000`. `health` và `catalog` không cần đăng nhập. `health` chỉ kiểm tra API, không xác nhận worker đang chạy.

Mã thoát: `0` thành công, `1` lỗi API/kết nối/file, `2` sai cú pháp hoặc tham số, `130` khi Ctrl+C. Thông báo lỗi và idempotency key đi ra stderr, dữ liệu đi ra stdout. Request có timeout 30 giây và không tự động thử lại thao tác thay đổi dữ liệu.

## Kiểm thử

Từ gốc repository, sau khi cài CLI:

```bash
python -m unittest discover -s cli/tests -v
ruff check cli
ruff format --check cli
mypy cli/spulti_cli
```

Test dùng HTTP server trên localhost, không cần control plane hoặc credentials cloud.
