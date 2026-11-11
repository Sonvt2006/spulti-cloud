# Tạo và quản lý máy spot

Instance Manager nhận yêu cầu từ CLI/dashboard, tạo VM trên AWS/GCP và theo dõi
trạng thái máy. Hướng dẫn dưới đây dùng **AWS trước** để dễ chạy thử.

## 1. Chuẩn bị

Trước khi tạo máy, cần có:

- Control plane đang chạy: [deploy EC2](README.md) hoặc [chạy local](README.local.md).
- Worker có AWS credentials và quyền tạo/xem/xóa EC2. **Policy S3 không cấp quyền tạo VM.**
- Catalog và giá spot thật: làm theo [Market Collector](Market_collector.md).
  Dữ liệu seed mẫu chưa đủ để tạo máy thật.
- Nếu chạy job có checkpoint/recovery: hoàn thành [S3 setup](S3_SETUP.md).

## 2. Chọn cấu hình máy AWS

Trên **EC2 control plane**, mở `control-plane/.env`. Nếu chạy control plane trên
laptop, dùng `.env.local`. Thêm hoặc sửa hai dòng sau:

```dotenv
SPULTI_MAX_MACHINES_PER_USER=5
SPULTI_LAUNCH_CONFIG='[{"provider":"AWS","region":"ap-southeast-1","zone":"ap-southeast-1a","image_id":"ami-REPLACE","machine_types":["m6i.xlarge"],"subnet_id":"subnet-REPLACE","security_group_ids":["sg-REPLACE"]}]'
```

`SPULTI_LAUNCH_CONFIG` là danh sách cấu hình mà server được phép dùng để tạo VM.
**Thay các giá trị mẫu trước khi chạy:**

| Trường | Điền gì? |
| --- | --- |
| `region` | Region tạo máy, ví dụ Singapore `ap-southeast-1` |
| `zone` | Availability Zone của subnet, ví dụ `ap-southeast-1a` |
| `image_id` | AMI ID của hệ điều hành cho VM mới; dùng Ubuntu 24.04 nếu chạy managed job |
| `machine_types` | Loại máy được phép tạo, ví dụ `m6i.xlarge`; phải có trong catalog và hợp kiến trúc AMI |
| `subnet_id` | Subnet ID trong đúng region/zone đã chọn |
| `security_group_ids` | Security Group ID cùng VPC với subnet; cho phép SSH từ IP của bạn |

AMI và network ở đây dành cho **VM spot sắp tạo**. VM cần đường mạng để tải agent
và gọi API. Không điền access key hoặc mật khẩu vào JSON này.
`SPULTI_MAX_MACHINES_PER_USER=5` giới hạn mỗi tài khoản tối đa 5 máy chưa xóa.

Nếu dùng GCP, thêm cấu hình sau vào cùng JSON array (ngăn cách các cấu hình bằng
dấu phẩy), rồi thay các giá trị mẫu và chuẩn bị credentials GCP cho worker:

```json
{"provider":"GCP","region":"asia-southeast1","zone":"asia-southeast1-b","image_id":"projects/PROJECT/global/images/IMAGE","machine_types":["e2-standard-4"],"project_id":"PROJECT","subnetwork":"projects/PROJECT/regions/asia-southeast1/subnetworks/SUBNET"}
```

Mỗi tổ hợp provider/region/zone/loại máy chỉ khai báo một lần.

## 3. Áp dụng cấu hình

Trên **EC2**, chạy từ thư mục `control-plane`:

```bash
docker compose build
docker compose run --rm --no-deps api python -m app.init_db
docker compose up -d --build
docker compose ps
```

Cần thấy `api`, `worker`, `beat` và `redis` đang chạy. Worker xử lý yêu cầu tạo/xóa;
Beat lên lịch kiểm tra máy. Chỉ chạy một Beat cho hệ thống.
Sau khi sửa `.env`, dùng `up -d` để nhận cấu hình mới; `restart` là chưa đủ.

Nếu chạy **local**, dùng các lệnh sau thay cho khối lệnh EC2:

```bash
docker compose --env-file .env.local -f compose.local.yaml build
docker compose --env-file .env.local -f compose.local.yaml up -d --wait postgres redis
docker compose --env-file .env.local -f compose.local.yaml run --rm --no-deps api python -m app.init_db
docker compose --env-file .env.local -f compose.local.yaml up -d --wait
```

## 4. Tạo thử một máy

Trên **máy cá nhân**, cài CLI và đăng nhập theo [CLI README](../cli/README.md).
Đăng ký SSH public key, rồi kiểm tra có máy phù hợp hay chưa:

```bash
spulti ssh-key add laptop --file ~/.ssh/id_ed25519.pub
spulti placement --vcpu 4 --ram-gib 16 --provider AWS --region singapore
```

Placement chỉ tìm ứng viên, chưa tạo VM. Nếu có kết quả và đã cấu hình bước 2,
tạo máy thử (**bắt đầu phát sinh chi phí cloud**):

```bash
spulti machine create training --vcpu 4 --ram-gib 16 --provider AWS --region singapore --disk-size-gb 30
spulti machine list
spulti machine show 1
```

Thay `1` bằng ID nhận được khi tạo máy. Yêu cầu được xử lý ở nền; xem lại
`machine show` để theo dõi:

| Trạng thái | Ý nghĩa |
| --- | --- |
| `PENDING` | Đang chờ xử lý |
| `PROVISIONING` | Đang tạo hoặc khởi động VM |
| `RUNNING` | Cloud báo máy đang chạy; SSH/agent có thể còn đang cài đặt |
| `RECOVERING` | Đang xử lý mất máy và cấp VM thay thế |
| `DELETED` | Đã xử lý xong yêu cầu xóa máy |

Khi VM sẵn sàng, SSH bằng `ssh_username` và `instance.public_ip` trong kết quả.
Tạo máy ở bước này chưa tự chạy workload. Để gắn job và tự khôi phục checkpoint,
làm tiếp [Recovery](RECOVERY.md); cần API và link tải agent truy cập được qua HTTPS.

## 5. Xem lỗi và xóa máy thử

```bash
spulti machine show 1
spulti machine events 1
spulti machine instances 1
```

Nếu `needs_attention=true`, đọc `last_error`, sửa nguyên nhân rồi yêu cầu thử lại:

```bash
spulti machine retry 1
```

| Vấn đề | Cần kiểm tra |
| --- | --- |
| Không có máy phù hợp | Catalog/giá đã nạp; yêu cầu CPU/RAM và region phù hợp cấu hình bước 2 |
| Cấu hình launch thiếu/sai | JSON trong `.env`, các ID mẫu đã thay, container đã recreate |
| Lỗi quyền AWS | Credentials/quyền EC2 của worker; quyền S3 thôi chưa đủ |
| Lỗi capacity/quota | Cloud còn máy spot và quota cho loại máy đã chọn hay không |
| Máy nằm mãi ở `PENDING` | Worker, Beat và Redis có đang chạy không |
| Có VM nhưng job không chạy | Xem [Recovery](RECOVERY.md), HTTPS API/release và [S3 setup](S3_SETUP.md) |

Xem log trên EC2, từ thư mục `control-plane`:

```bash
docker compose logs --tail=100 worker beat
```

Kết thúc thử nghiệm, yêu cầu xóa và kiểm tra đến khi trạng thái là `DELETED`:

```bash
spulti machine delete 1
spulti machine show 1
```

`docker compose down` không xóa VM trên cloud. Máy tạo bằng các script thử adapter
cũ phải quản lý bằng script đó; chúng không tự xuất hiện thành máy do Instance Manager quản lý.

API tương ứng có tại `/docs` của control plane: `/api/machines` để tạo/liệt kê,
`/api/machines/{id}` để xem/xóa, và các đường dẫn con `/retry`, `/instances`, `/events`.
Nếu mất phản hồi khi tạo máy, dùng lại cùng `Idempotency-Key` và tham số theo
[CLI README](../cli/README.md) để tránh gửi yêu cầu tạo máy mới.
