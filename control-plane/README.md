# Spulti-cloud Control Plane

Hệ thống Control Plane xử lý tác vụ bất đồng bộ (Asynchronous Task Queue) sử dụng **FastAPI**, **Celery**, **Redis**, **Nginx Reverse Proxy** và cơ sở dữ liệu **AWS RDS PostgreSQL**, được đóng gói và điều phối hoàn toàn bằng **Docker Compose**.

---

## 1. Kiến trúc hệ thống

Hệ thống bao gồm 4 dịch vụ container hoạt động trong mạng nội bộ của Docker:

* **`nginx` (`nginx:stable-alpine`)**: Cổng giao tiếp duy nhất mở ra ngoài ở cổng `80` (`80:80`), giới hạn dung lượng request tối đa `1m` (`client_max_body_size 1m`) và điều hướng lưu lượng (`proxy_pass`) vào dịch vụ `api` ở cổng `8000`.
* **`api` (`spulti-control-plane:dev`)**: Ứng dụng FastAPI chạy trên ASGI server `uvicorn` tại cổng `8000` dưới quyền người dùng phi đặc quyền `appuser` (UID `10001`) trên nền `python:3.12-slim`.
* **`worker` (`spulti-control-plane:dev`)**: Tiến trình Celery Worker (`--concurrency=2`, `--loglevel=INFO`) thực thi các tác vụ chạy ngầm từ hàng đợi Redis với thời gian chờ tắt an toàn `stop_grace_period: 60s`.
* **`redis` (`redis:7-alpine`)**: Đóng vai trò là Message Broker (`redis://redis:6379/0`) và Result Backend (`redis://redis:6379/1`), được bật cơ chế lưu trữ bền vững AOF (`--appendonly yes`, `--appendfsync everysec`) và chính sách bộ nhớ `--maxmemory-policy noeviction` gắn với volume `redis_data`.

---

## 2. Cấu trúc thư mục dự án

Đảm bảo mã nguồn trong repository có cấu trúc thư mục khớp với khai báo trong `Dockerfile` và `compose.yaml`:

```text
.
├── app/
│   ├── __init__.py
│   ├── celery_app.py      # Cấu hình kết nối Celery với Redis Broker (DB 0) & Backend (DB 1)
│   ├── database.py        # Kết nối SQLAlchemy tới RDS PostgreSQL qua biến DATABASE_URL
│   ├── models.py          # Nơi duy nhất định nghĩa các bảng của database
│   ├── init_db.py         # Script tạo bảng: python -m app.init_db
│   ├── main.py            # Các HTTP endpoint của FastAPI (/health, /api/tasks/...)
│   ├── auth.py            # Đăng ký, đăng nhập (/api/auth/...) và dependency get_current_user
│   ├── security.py        # Băm mật khẩu (argon2) và tạo/kiểm tra JWT
│   └── tasks.py           # Định nghĩa các Celery background task (spulti.demo_add)
├── db/
│   └── seed.sql           # Dữ liệu mẫu để thử nghiệm
├── nginx/
│   └── default.conf       # Cấu hình Nginx Reverse Proxy lắng nghe cổng 80 -> api:8000
├── .env.example          # Mẫu file .env chứa DATABASE_URL
├── .dockerignore          # Loại trừ .git, .venv, .env, __pycache__, *.pyc, *.pem khi build
├── Dockerfile             # Công thức đóng gói image Python 3.12 cho api và worker
├── compose.yaml           # Điều phối 4 dịch vụ: redis, api, worker, nginx
├── requirements.txt       # Danh sách các thư viện Python cần cài đặt
└── README.md
```

---

## 3. Yêu cầu chuẩn bị trên AWS EC2

1. **Khởi tạo EC2 Instance**:
   * Hệ điều hành khuyến nghị: **Ubuntu Server 22.04 LTS / 24.04 LTS** (hoặc Amazon Linux 2023).
   * Cấu hình tối thiểu: `t3.small` hoặc `t2.micro` / `t3.micro` (tối thiểu 1 vCPU, 1–2 GB RAM).
2. **Cấu hình Security Group (Firewall của EC2)**:
   * Mở cổng **22 (SSH)**: Cho phép IP của bạn kết nối quản trị từ xa.
   * Mở cổng **80 (HTTP)**: Cho phép lưu lượng truy cập vào `nginx` (`0.0.0.0/0` hoặc dải IP nội bộ).
   * *Lưu ý*: **Không** cần mở cổng `8000` (FastAPI) hay `6379` (Redis) trên Security Group vì các cổng này chỉ giao tiếp trong mạng nội bộ Docker.
3. **Khởi tạo RDS PostgreSQL** (RDS → Create database):
   * Engine: **PostgreSQL 16** trở lên, template **Free tier** hoặc **Dev/Test**, instance `db.t3.micro` / `db.t4g.micro`.
   * DB instance identifier: `spulti-db`, master username: `spulti_admin`, đặt mật khẩu mạnh.
   * *Additional configuration* → **Initial database name**: `spulti` (nếu bỏ trống, RDS sẽ không tạo database này).
   * Đặt cùng VPC với EC2, **Public access: No**.
   * Security Group của RDS: chỉ mở cổng **5432** với nguồn là **Security Group của EC2** (không mở `0.0.0.0/0`).
   * Sau khi tạo xong, lấy **Endpoint** trong tab *Connectivity & security* (dạng `spulti-db.xxxx.<region>.rds.amazonaws.com`).

### Cấu trúc database

Tất cả các bảng được định nghĩa trong `app/models.py`. Muốn thêm hay sửa bảng thì sửa ở file này, không tạo file `.sql` riêng. Mọi bảng dùng `id` kiểu số nguyên tự tăng (1, 2, 3, ...).

| Bảng | Nội dung chính |
|------|----------------|
| `users` | `name`, `email` và `username` (unique), `password_hash`, `is_active`, `created_at`, `updated_at` |
| `ssh_keys` | `user_id` → `users.id`, `name`, `public_key`, `fingerprint`; mỗi user không trùng `name` hay `fingerprint` |
| `api_keys` | `user_id` → `users.id`, `name`, `key_prefix`, `key_hash` (SHA-256, unique), `last_used_at`, `expires_at`, `revoked_at` |
| `machine_types` | Catalog loại máy AWS/GCP: `provider`, `name`, `vcpu`, `ram_gib`, `gpu_count` |
| `machines` | Máy Spulti của người dùng: `user_id` → `users.id`, cấu hình yêu cầu, `optimization_mode` (`SAVING`/`STABLE`/`BALANCED`), `status` (`PENDING`/`PROVISIONING`/`RUNNING`/`RECOVERING`/`DELETED`) |
| `instances` | Máy thật trên cloud gắn với một máy Spulti: `machine_id`, `machine_type_id`, `provider_instance_id`, `region`, `zone`, `started_at`, `ended_at` |
| `spot_prices` | Lịch sử giá: `machine_type_id`, `region`, `zone`, `price_type` (`SPOT`/`ON_DEMAND`), `price_per_hour`, `recorded_at` |
| `events` | Sự kiện thu hồi: `machine_id`, `instance_id`, `event_type` (`RECLAIM`/`INTERRUPT`), `details` (JSON) |
| `cost_records` | Chi phí mỗi instance: `actual_cost`, `on_demand_cost` |

Xóa một user sẽ xóa luôn SSH key và API key của user đó (`ON DELETE CASCADE`). API key gốc không được lưu, chỉ lưu hash và vài ký tự đầu để hiển thị.

---

## 4. Hướng dẫn cài đặt trên EC2

### Bước 1: Kết nối SSH vào EC2
```bash
ssh -i /duong/dan/toi/key.pem ubuntu@<EC2_PUBLIC_IP>
```

### Bước 2: Cài đặt Docker Engine và Docker Compose Plugin (Ubuntu)
```bash
# Cập nhật danh sách gói phần mềm và cài đặt các gói phụ thuộc
sudo apt-get update
sudo apt-get install -y ca-certificates curl git

# Thêm GPG key chính thức của Docker
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# Thêm repository của Docker vào Apt sources
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Cài đặt Docker Engine và Docker Compose plugin
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Cấp quyền chạy Docker cho user hiện tại mà không cần gõ sudo
sudo usermod -aG docker $USER
newgrp docker
```

Kiểm tra phiên bản sau khi cài đặt:
```bash
docker --version
docker compose version
```

### Bước 3: Tải mã nguồn dự án về EC2
```bash
git clone https://github.com/Sonvt2006/spulti-cloud.git spulti-cloud
cd spulti-cloud/control-plane
```

### Bước 4: Cấu hình kết nối RDS và khóa JWT
Tạo file `.env` từ mẫu rồi thay `<password>` và `<rds-endpoint>` bằng thông tin RDS ở mục 3, và đặt `JWT_SECRET` bằng một chuỗi ngẫu nhiên dài ít nhất 32 ký tự 

```bash
cp .env.example .env
nano .env
```
Tạo JWT_SECRET bằng:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

`sslmode=require` bắt buộc kết nối tới RDS phải được mã hóa. Nếu mật khẩu có ký tự đặc biệt (`@`, `:`, `/`, `#`, ...), cần URL-encode trước khi đặt vào `DATABASE_URL`. `docker compose` sẽ báo lỗi và dừng nếu chưa đặt `DATABASE_URL` hoặc `JWT_SECRET`. Đổi `JWT_SECRET` sẽ làm mọi token đang dùng mất hiệu lực.

Tạo các bảng trong database (chỉ cần chạy lần đầu, chạy lại cũng không sao vì chỉ tạo bảng còn thiếu):

```bash
docker compose build
docker compose run --rm --no-deps api python -m app.init_db
```

Nếu muốn có dữ liệu mẫu để thử nghiệm, nạp `db/seed.sql` (chỉ chạy một lần, chạy lại sẽ báo lỗi trùng dữ liệu):

```bash
set -a; . ./.env; set +a
docker run --rm -i postgres:16-alpine psql "${DATABASE_URL/+psycopg/}" -v ON_ERROR_STOP=1 < db/seed.sql
```

### Bước 5: Khởi chạy hệ thống bằng Docker Compose
Build image `spulti-control-plane:dev` và khởi chạy toàn bộ 4 dịch vụ ở chế độ chạy ngầm (`-d`):

```bash
docker compose up -d --build
```

Quá trình khởi động sẽ tự động diễn ra theo thứ tự phụ thuộc (`depends_on` kèm `healthcheck`):
1. `redis` khởi chạy trước và kiểm tra sức khỏe qua lệnh `redis-cli ping`.
2. Khi `redis` đạt trạng thái `healthy`, `api` và `worker` bắt đầu khởi chạy.
3. Khi `api` vượt qua bài kiểm tra sức khỏe tại `http://127.0.0.1:8000/health`, `nginx` sẽ khởi chạy và mở cổng `80`.

Kiểm tra trạng thái các container:
```bash
docker compose ps
```

---

## 5. Kiểm tra hoạt động của API (Verification)

Thay `<EC2_PUBLIC_IP>` bằng địa chỉ IP Public của máy chủ EC2 (hoặc `localhost` nếu chạy lệnh ngay bên trong máy EC2):

### 1. Kiểm tra Healthcheck của API
```bash
curl -i http://<EC2_PUBLIC_IP>/health
```
**Kết quả mong đợi (`200 OK`)**:
```json
{"status":"ok","service":"spulti-api"}
```

### 2. Đăng ký, đăng nhập và gọi API bằng token

Đăng ký tài khoản (`email` và `username` được chuyển về chữ thường; mật khẩu từ 8 đến 128 ký tự):
```bash
curl -X POST http://<EC2_PUBLIC_IP>/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"name": "Peter Parker", "email": "peter@example.com", "username": "peter", "password": "spiderman"}'
```
**Kết quả mong đợi (`201 Created`)**; trùng email hoặc username trả `409`:
```json
{"id": 1, "name": "Peter Parker", "email": "peter@example.com", "username": "peter"}
```

Đăng nhập bằng username hoặc email để nhận JWT (hết hạn sau `JWT_EXPIRE_MINUTES` phút, mặc định 60):
```bash
curl -X POST http://<EC2_PUBLIC_IP>/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "peter", "password": "spiderman"}'
```
**Kết quả mong đợi (`200 OK`)**; sai thông tin trả `401`:
```json
{"access_token": "eyJhbGciOi...", "token_type": "bearer", "expires_in": 3600}
```

Gửi token trong header `Authorization` để gọi các endpoint cần đăng nhập:
```bash
curl http://<EC2_PUBLIC_IP>/api/auth/me -H "Authorization: Bearer <ACCESS_TOKEN>"
```
Thiếu token, token sai, hết hạn hoặc tài khoản bị khóa (`is_active = false`) đều trả `401`.

---

### 3. Gửi một tác vụ tính toán bất đồng bộ vào hàng đợi (phải đăng nhập có <ACCESS_TOKEN> mới gửi task được)
Gửi request `POST /api/tasks/demo` với hai số nguyên `a` và `b` (nằm trong khoảng `-1,000,000` đến `1,000,000`):
```bash
curl -X POST http://<EC2_PUBLIC_IP>/api/tasks/demo \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <ACCESS_TOKEN>"
  -d '{"a": 15, "b": 27}'
```
**Kết quả mong đợi (`202 Accepted`)**:
```json
{
  "task_id": "c7a6b8e0-1234-4567-89ab-cdef01234567",
  "status": "QUEUED",
  "status_url": "/api/tasks/c7a6b8e0-1234-4567-89ab-cdef01234567"
}
```

### 4. Truy vấn trạng thái và kết quả của tác vụ
Sử dụng `task_id` (hoặc `status_url`) nhận được ở bước trên để kiểm tra tiến độ xử lý của Celery Worker (task `spulti.demo_add` được giả lập độ trễ `5 giây`):

```bash
curl http://<EC2_PUBLIC_IP>/api/tasks/<TASK_ID> -H "Authorization: Bearer <ACCESS_TOKEN>"
```

* **Khi task đang được thực thi**:
  ```json
  {
    "task_id": "c7a6b8e0-1234-4567-89ab-cdef01234567",
    "status": "STARTED"
  }
  ```
* **Sau 5 giây khi task hoàn tất (`SUCCESS`)**:
  ```json
  {
    "task_id": "c7a6b8e0-1234-4567-89ab-cdef01234567",
    "status": "SUCCESS",
    "result": {
      "operation": "demo_add",
      "result": 42,
      "executed_by": "celery-worker"
    }
  }
  ```

## 6. Tạo/Xem/Xóa spot instance thông qua AWS và GCP adapter

Git pull code mới nhất về và chạy docker build để có môi trường mới nhất. (CHẠY TRÊN MÁY EC2)

### 1. Kiểm tra thư viện trong worker
```bash
docker compose exec worker python -c \
"import boto3; from google.cloud import compute_v1; print('AWS/GCP SDK OK')"
```

### 2. Kiểm tra danh tính AWS
```bash
docker compose exec worker python -c \
"import boto3; r=boto3.client('sts', region_name='us-east-1').get_caller_identity(); print(r['Account']); print(r['Arn'])"
```
### 3. Kiểm tra GCP
```bash
docker compose exec worker python -c \
"from google.cloud import compute_v1; c=compute_v1.InstancesClient(); next(iter(c.list(project='spulti-cloud', zone='asia-southeast1-b')), None); print('GCP read OK')"
```
### 4. Chạy tạo, xem và xóa Spot instance
AWS:
```bash
docker compose exec worker python -m app.scripts.try_aws_adapter create

docker compose exec worker python -m app.scripts.try_aws_adapter status

docker compose exec worker python -m app.scripts.try_aws_adapter delete
```

GCP: (chú ý trước khi chạy vào file /control-plane/app/scripts/try_gcp_adapter.py sửa USER theo tên và CREATE_GENERATION theo số lần đã chạy)
```bash
docker compose exec worker python -m app.scripts.try_gcp_adapter create

docker compose exec worker python -m app.scripts.try_gcp_adapter status

docker compose exec worker python -m app.scripts.try_gcp_adapter delete
```

## 7. Các lệnh vận hành và bảo trì thường dùng

* **Xem log toàn bộ hệ thống theo thời gian thực**:
  ```bash
  docker compose logs -f
  ```
* **Xem log riêng của `worker` hoặc `api`**:
  ```bash
  docker compose logs -f worker
  docker compose logs -f api
  ```
* **Cập nhật phiên bản mới khi có thay đổi code trên GitHub**:
  ```bash
  git pull origin main
  docker compose up -d --build
  ```
* **Khởi động lại một dịch vụ cụ thể**:
  ```bash
  docker compose restart worker
  ```
* **Dừng toàn bộ hệ thống (vẫn giữ lại dữ liệu Redis trong volume `redis_data`)**:
  ```bash
  docker compose down
  ```
* **Dừng hệ thống và xóa sạch dữ liệu Redis volume**:
  ```bash
  docker compose down -v
  ```
