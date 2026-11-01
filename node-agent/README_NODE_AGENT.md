# Spulti-cloud — Node Agent

> Tài liệu mô tả kiến trúc, nhiệm vụ từng file và cách chạy thử **Node Agent kết nối Control Plane** trong dự án Spulti-cloud. Control Plane và Node Agent là hai chương trình độc lập, được đặt ở hai thư mục ngang cấp trong cùng repository.

Node Agent chịu trách nhiệm chạy workload trên máy thực thi (Spot VM AWS/GCP khi triển khai thực tế), gửi heartbeat về Control Plane và lưu checkpoint để có thể tiếp tục tác vụ sau khi tiến trình hoặc VM bị dừng. Control Plane cung cấp FastAPI để tạo job, tiếp nhận trạng thái và lưu checkpoint JSON vào PostgreSQL (local hoặc RDS).

---

## 1. Kiến trúc hệ thống

```text
                    Người dùng / CLI
                            |
                   JWT / User API Key
                            |
                            v
             +---------------------------+
             |       Control Plane       |
             |        FastAPI            |
             |   /api/agent/jobs/...     |
             +---------------------------+
                 |                 ^
                 |                 | HTTPS + X-Agent-Token
                 v                 |
          +-------------+    +--------------------------+
          | PostgreSQL  |    |       Node Agent         |
          | / AWS RDS   |    | chạy trên Spot VM AWS/GCP |
          | agent_jobs  |    | - thực thi workload       |
          | checkpoint  |    | - gửi heartbeat           |
          +-------------+    | - lưu/tải checkpoint      |
                             +--------------------------+

Khi VM/tiến trình bị dừng:
    Agent trên VM khác -> tải checkpoint từ Control Plane
                         -> chạy tiếp từ bước kế tiếp
```

| Thành phần | Nhiệm vụ | Nơi chạy |
|---|---|---|
| **Control Plane (FastAPI)** | Tạo job, xác thực Agent, nhận heartbeat, lưu/đọc checkpoint | EC2 quản lý hoặc Docker local |
| **PostgreSQL / RDS** | Lưu bảng `agent_jobs`, tiến độ và checkpoint JSON | PostgreSQL local hoặc AWS RDS |
| **Node Agent** | Thực thi workload, duy trì trạng thái và khôi phục công việc | Laptop khi test; Spot VM khi triển khai |
| **Celery Worker / Beat** | Các tác vụ bất đồng bộ và lịch Market Collector của Control Plane | EC2 quản lý; không thực thi job demo của Agent |
| **Redis** | Message Broker của Celery | Control Plane; không nằm trên đường truyền heartbeat Agent |

### Luồng hoạt động

1. Người dùng đăng nhập Control Plane và tạo job thông qua `POST /api/agent/jobs`.
2. Control Plane lưu cấu hình job vào bảng `agent_jobs`, trả về `job_id` và `agent_token`.
3. Node Agent sử dụng token riêng của job để lấy cấu hình và checkpoint qua API.
4. Agent thực thi workload, định kỳ lưu checkpoint local và đồng bộ checkpoint lên Control Plane; đồng thời gửi heartbeat.
5. Nếu tiến trình bị dừng, Agent khác có thể dùng cùng `job_id` và token để tải checkpoint từ Control Plane, rồi tiếp tục từ bước đã lưu.
6. Khi job hoàn thành, Agent gửi checkpoint cuối và trạng thái `COMPLETED`.

**Phạm vi demo:** Workload hiện tại là vòng lặp đếm bước. Việc chạy trên một thư mục state mới mô phỏng VM thay thế, **không đồng nghĩa Control Plane đã có chức năng tự provision Spot VM**.

---

## 2. Cấu trúc thư mục dự án

```text
spulti-cloud/
├── control-plane/                     # Backend điều khiển hệ thống
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                    # Khởi tạo FastAPI và đăng ký agent_router
│   │   ├── agent_api.py               # REST API quản lý job, heartbeat, checkpoint
│   │   ├── models.py                  # SQLAlchemy models, gồm AgentJob
│   │   ├── init_db.py                 # Tạo các bảng chưa có bằng SQLAlchemy metadata
│   │   ├── database.py                # Kết nối PostgreSQL / RDS
│   │   ├── auth.py                    # Xác thực JWT / API key người dùng
│   │   ├── api_keys.py                # Quản lý API key của người dùng
│   │   ├── celery_app.py              # Cấu hình Celery
│   │   └── ...                        # Catalog, Placement, Adapters, Tasks
│   ├── tests/
│   │   ├── agent_e2e_server.py        # FastAPI server thử nghiệm + SQLite tạm
│   │   └── test_node_agent_integration.py  # Test Agent <-> HTTP API
│   ├── compose.yaml                  # Docker Compose trên EC2 / RDS
│   ├── compose.local.yaml            # Docker Compose trên Windows local
│   ├── Dockerfile
│   └── requirements.txt
│
├── node-agent/                        # Chương trình chạy trên máy thực thi
│   ├── agent.py                       # Entrypoint của Agent
│   ├── config.py                      # Tham số, cấu hình kết nối
│   ├── job_runner.py                  # Chạy job, heartbeat, checkpoint, resume
│   ├── checkpoint.py                  # Đọc/ghi checkpoint local
│   ├── heartbeat.py                   # Ghi heartbeat local
│   ├── control_plane.py               # HTTP client gọi Control Plane
│   ├── requirements.txt               # Phụ thuộc Python của Agent
│   ├── .gitignore                     # Loại trừ state và cache
│   └── tests/
│       └── test_recovery.py           # Unit tests checkpoint và khôi phục
│
└── README.md
```

Hai chương trình sử dụng cùng repository nhưng có **vòng đời và môi trường thực thi riêng**. Node Agent không cần trực tiếp kết nối RDS và không cần chạy chung Docker container với FastAPI.

---

## 3. Giải thích các file trong `node-agent/`

### 3.1. `agent.py` — Điểm bắt đầu chương trình

Đây là file được chạy trực tiếp trên máy thực thi:

```bash
python agent.py --job-id demo-job-001 --total-steps 1000
```

File gọi `parse_args()` trong `config.py` để đọc cấu hình, sau đó gọi `run_job()` trong `job_runner.py`. Nếu cấu hình hoặc quá trình chạy có lỗi, chương trình trả mã lỗi thích hợp.

### 3.2. `config.py` — Cấu hình Node Agent

Định nghĩa `AgentConfig` và kiểm tra các đối số khi chạy:

| Tham số | Ý nghĩa |
|---|---|
| `--job-id` | ID của job; khi kết nối API lấy từ Control Plane |
| `--total-steps` | Tổng số bước của workload demo |
| `--checkpoint-every` | Lưu checkpoint sau mỗi N bước |
| `--heartbeat-every` | Gửi heartbeat sau mỗi N bước |
| `--step-delay` | Thời gian chờ mỗi bước, tính bằng giây |
| `--state-dir` | Thư mục lưu checkpoint và heartbeat local |
| `--control-plane-url` | Địa chỉ Control Plane API |
| `--simulate-crash-at` | Chỉ dùng trong thử nghiệm để mô phỏng crash tại một bước |
| `SPULTI_AGENT_TOKEN` | Biến môi trường chứa token xác thực dành riêng cho job |

Chế độ kết nối API yêu cầu có đồng thời URL và Agent token. Các tham số `total_steps`, `checkpoint_every`, `heartbeat_every` phải khớp cấu hình job được tạo trên server.

### 3.3. `job_runner.py` — Điều phối vòng đời job

Chứa hàm `run_job(config)` và toàn bộ luồng thực thi:

- Khởi tạo `CheckpointStore`, `HeartbeatStore` và `ControlPlaneClient` khi có cấu hình remote.
- Đọc checkpoint local, hỏi checkpoint server và chọn tiến độ phù hợp.
- Thực thi vòng lặp từ `last_completed_step + 1`.
- Gọi `checkpoint_now()` và `heartbeat_now()` theo chu kỳ cấu hình.
- Lưu checkpoint cuối và gửi heartbeat `COMPLETED` khi hoàn thành.
- Xử lý tín hiệu dừng `SIGINT`/`SIGTERM` theo cơ chế best-effort.

Workload demo **không tự lưu bộ nhớ của một chương trình bất kỳ**. Khi tích hợp training thật, chương trình training cần tự cung cấp trạng thái có thể serialize/restore.

### 3.4. `checkpoint.py` — Lưu và tải checkpoint

`CheckpointStore` đảm nhiệm việc ghi, đọc và kiểm tra cấu trúc checkpoint. Hàm `write_json_atomic()` ghi qua file tạm trước khi thay thế file chính để hạn chế checkpoint JSON bị ghi dở.

Checkpoint local được lưu tại:

```text
<state-dir>/checkpoints/<job-id>.json
```

Ví dụ một checkpoint của job demo:

```json
{
  "version": 1,
  "job_id": "demo-job-001",
  "total_steps": 100,
  "last_completed_step": 30,
  "status": "RUNNING",
  "updated_at": "2026-11-01T10:00:00+00:00"
}
```

Giá trị `last_completed_step` cho biết bước cuối **đã được lưu**, không nhất thiết bằng bước vừa thực thi tại thời điểm VM bị dừng.

### 3.5. `heartbeat.py` — Ghi trạng thái local

`HeartbeatStore.write()` ghi file JSON chứa `job_id`, PID, `current_step`, `last_checkpoint_step`, `status` và timestamp.

```text
<state-dir>/heartbeats/<job-id>.json
```

File này quản lý heartbeat **local**. Việc gửi heartbeat qua HTTP do `job_runner.py` gọi thông qua `ControlPlaneClient`.

### 3.6. `control_plane.py` — HTTP client

`ControlPlaneClient` quản lý giao tiếp giữa Node Agent và FastAPI:

| Phương thức | Chức năng |
|---|---|
| `verify_config()` | Xác minh cấu hình job với server |
| `get_checkpoint()` | Tải checkpoint gần nhất từ Control Plane |
| `save_checkpoint()` | Đồng bộ checkpoint JSON lên server |
| `send_heartbeat()` | Gửi tiến độ và trạng thái Agent |

Client đính kèm `X-Agent-Token` trong request và có retry cho một số lỗi kết nối tạm thời. HTTP chỉ được dùng trên loopback (`localhost`, `127.0.0.1`); khi Agent truy cập Control Plane từ Spot VM phải dùng **HTTPS** để bảo vệ token.

### 3.7. `requirements.txt`, `.gitignore` và `tests/test_recovery.py`

- `requirements.txt`: bản Agent demo sử dụng thư viện chuẩn Python 3.11+, không yêu cầu FastAPI/Celery/SQLAlchemy trên VM.
- `.gitignore`: bỏ qua thư mục trạng thái local và file cache Python.
- `tests/test_recovery.py`: kiểm tra chạy hoàn thành, crash rồi resume, cấu hình checkpoint không khớp và checkpoint bị hỏng.

---

## 4. Các file liên quan trong `control-plane/app/`

### 4.1. `agent_api.py` — REST API cho Node Agent

Router `/api/agent` có các endpoint:

| Method | Đường dẫn | Xác thực | Chức năng |
|---|---|---|---|
| `POST` | `/api/agent/jobs` | Bearer JWT/API key người dùng | Tạo job và trả Agent token |
| `GET` | `/api/agent/jobs/{job_id}` | Bearer của chủ job | Xem trạng thái job |
| `GET` | `/api/agent/jobs/{job_id}/config` | `X-Agent-Token` | Đọc cấu hình job |
| `GET` | `/api/agent/jobs/{job_id}/checkpoint` | `X-Agent-Token` | Tải checkpoint |
| `PUT` | `/api/agent/jobs/{job_id}/checkpoint` | `X-Agent-Token` | Lưu checkpoint mới |
| `POST` | `/api/agent/jobs/{job_id}/heartbeat` | `X-Agent-Token` | Cập nhật heartbeat |

Agent token chỉ trả về khi tạo job; database giữ mã băm SHA-256, không lưu token gốc. API kiểm tra quyền sở hữu, tính hợp lệ của số bước và ngăn checkpoint cũ ghi đè checkpoint mới hơn. Bản demo hỗ trợ checkpoint JSON tối đa **16 KiB**.

Trong ứng dụng, `AgentJob` được sử dụng thông qua:

```python
from app.models import AgentJob
```

### 4.2. `models.py` — Lưu cấu trúc dữ liệu

Model `AgentJob` đăng ký bảng `agent_jobs` bằng SQLAlchemy, cùng `Base` với các model `User`, `Machine`, `Instance`, `SpotPrice` và những bảng khác của Control Plane.

| Trường | Ý nghĩa |
|---|---|
| `id` | Định danh job dạng UUID string |
| `user_id` | ID người dùng sở hữu job, khóa ngoại đến `users` |
| `agent_token_hash` | Hash của token Agent |
| `total_steps` | Tổng số bước cần chạy |
| `checkpoint_every`, `heartbeat_every` | Chu kỳ lưu/gửi tính theo số bước |
| `status` | `PENDING`, `RUNNING`, `INTERRUPTED`, `COMPLETED` |
| `current_step` | Bước mới nhất được Agent báo |
| `checkpoint_step` | Bước gần nhất có checkpoint bền vững trên server |
| `checkpoint_data` | Nội dung checkpoint JSON |
| `last_heartbeat_at` | Thời điểm Control Plane nhận heartbeat gần nhất |
| `created_at`, `updated_at` | Thời gian tạo và cập nhật job |

`current_step` có thể lớn hơn `checkpoint_step`: Agent có thể đã xử lý thêm các bước nhưng chưa đến thời điểm lưu checkpoint tiếp theo.

### 4.3. `init_db.py` — Khởi tạo database

`init_db.py` import các model để đăng ký SQLAlchemy metadata, sau đó tạo các bảng còn thiếu:

```python
from app import models  # noqa: F401
from app.database import Base, engine


def main() -> None:
    Base.metadata.create_all(engine)
    print("Tables:", ", ".join(Base.metadata.tables))


if __name__ == "__main__":
    main()
```

Thực thi từ thư mục `control-plane` bằng `python -m app.init_db` trong đúng môi trường có cấu hình `DATABASE_URL`. `create_all` **không thay thế công cụ migration** nếu sau này cần sửa cấu trúc một bảng đã tồn tại.

### 4.4. `main.py`, `database.py`, `auth.py` và `api_keys.py`

- `main.py`: tạo ứng dụng FastAPI và đưa router Node Agent vào hệ thống:

  ```python
  from app.agent_api import router as agent_router

  # Sau khi khởi tạo app = FastAPI(...)
  app.include_router(agent_router)
  ```

- `database.py`: cung cấp SQLAlchemy `engine` và dependency `get_db` để API thao tác trên PostgreSQL local hoặc RDS.
- `auth.py`: xác thực người dùng khi tạo hoặc xem job; chủ job được xác định từ tài khoản đăng nhập.
- `api_keys.py`: quản lý API key của người dùng dành cho ứng dụng/CLI, độc lập với token riêng theo job của Node Agent.

---

## 5. Kiểm thử: `control-plane/tests/` và `node-agent/tests/`

| File | Nhiệm vụ |
|---|---|
| `node-agent/tests/test_recovery.py` | Kiểm tra checkpoint và resume ở cấp Node Agent, không cần API |
| `control-plane/tests/agent_e2e_server.py` | Tạo một FastAPI server thử nghiệm, sử dụng SQLite tạm và xác thực test |
| `control-plane/tests/test_node_agent_integration.py` | Gọi HTTP API thật, khởi động hai tiến trình Agent và xác nhận checkpoint được khôi phục |

Trong cây thư mục trên, integration test lấy đường dẫn Node Agent từ **thư mục cùng cấp với Control Plane**:

```python
from pathlib import Path

API = Path(__file__).resolve().parents[1]  # spulti-cloud/control-plane
AGENT = API.parent / "node-agent" / "agent.py"
```

Test HTTP sử dụng SQLite và user test, **không dùng RDS, không gọi AWS/GCP, không tạo máy ảo**.

Mô phỏng kiểm thử chính:

```text
Agent 1: chạy đến bước 37 -> crash
    checkpoint đã lưu ở bước 30 trên Control Plane
Agent 2: thư mục state khác, ban đầu rỗng
    -> tải checkpoint bước 30
    -> chạy từ bước 31 đến bước 100
    -> trạng thái COMPLETED
```

---

## 6. Hướng dẫn chạy trên local (Windows)

### 6.1. Khởi động Control Plane

Điều kiện: Docker Desktop đang chạy; `control-plane/.env.local` và các file được `compose.local.yaml` mount đã được cấu hình theo dự án hiện có.

Mở PowerShell tại `spulti-cloud/control-plane`:

```powershell
docker compose --env-file .env.local -f compose.local.yaml config --quiet

docker compose --env-file .env.local -f compose.local.yaml up -d --build api

docker compose --env-file .env.local -f compose.local.yaml ps -a
```

Khi `api` đã chạy, tạo các bảng còn thiếu:

```powershell
docker compose --env-file .env.local -f compose.local.yaml exec api python -m app.init_db
```

Kiểm tra API:

```powershell
curl.exe http://127.0.0.1:8080/health
```

Mở Swagger tại `http://127.0.0.1:8080/docs` để xem nhóm endpoint `node-agent`.

Nếu API ở trạng thái `Exited (1)`, kiểm tra traceback **trước khi** chạy lệnh `exec`:

```powershell
docker compose --env-file .env.local -f compose.local.yaml logs --tail=100 api
```

### 6.2. Tạo một job thông qua FastAPI

1. Tại `/docs`, đăng nhập qua `POST /api/auth/login` để nhận `access_token` (hoặc dùng API key người dùng hợp lệ).
2. Chọn **Authorize** và nhập Bearer token theo hướng dẫn Swagger.
3. Chạy `POST /api/agent/jobs` với payload:

```json
{
  "total_steps": 100,
  "checkpoint_every": 10,
  "heartbeat_every": 7
}
```

API trả về `job_id` và `agent_token`. Lưu token ở nơi an toàn để thực hiện hai lượt Agent; **không commit hoặc chụp ảnh công khai token**.

### 6.3. Chạy Node Agent và mô phỏng crash

Mở PowerShell khác tại **`spulti-cloud/node-agent`**:

```powershell
$env:SPULTI_AGENT_TOKEN = "AGENT_TOKEN_TU_API"

python agent.py `
  --job-id "JOB_ID_TU_API" `
  --total-steps 100 `
  --checkpoint-every 10 `
  --heartbeat-every 7 `
  --step-delay 0 `
  --state-dir .\state-vm1 `
  --control-plane-url http://127.0.0.1:8080 `
  --simulate-crash-at 37
```

Lần chạy thứ nhất cố ý thoát với **exit code 75** để mô phỏng VM/tiến trình bị mất. Checkpoint cuối được đồng bộ là **bước 30**.

### 6.4. Khôi phục với một Agent mới

Vẫn trong `spulti-cloud/node-agent`, sử dụng cùng `job_id` và token nhưng một thư mục trạng thái **khác**:

```powershell
python agent.py `
  --job-id "JOB_ID_TU_API" `
  --total-steps 100 `
  --checkpoint-every 10 `
  --heartbeat-every 7 `
  --step-delay 0 `
  --state-dir .\state-vm2 `
  --control-plane-url http://127.0.0.1:8080

Remove-Item Env:SPULTI_AGENT_TOKEN
```

Kết quả mong đợi:

```text
[agent] RESTORED_FROM_CONTROL_PLANE step=30
[agent] job=... RESUME_FROM=30 NEXT_STEP=31 TOTAL=100
[agent] COMPLETED job=... steps=100
```

Có thể xem kết quả bằng `GET /api/agent/jobs/{job_id}` trên Swagger: `status = COMPLETED`, `checkpoint_step = 100`.

### 6.5. Kiểm tra bảng `agent_jobs` trong PostgreSQL local

Từ thư mục `control-plane`:

```powershell
docker compose --env-file .env.local -f compose.local.yaml exec postgres psql -U spulti -d spulti -c 'SELECT id, status, current_step, checkpoint_step, last_heartbeat_at FROM agent_jobs ORDER BY created_at DESC LIMIT 5;'
```

### 6.6. Chạy bộ test tự động

Unit test được chạy tại `spulti-cloud/node-agent`:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Integration test được chạy tại `spulti-cloud/control-plane` bằng `.venv` có các thư viện FastAPI, SQLAlchemy, Uvicorn và dependencies của dự án:

```powershell
python -m unittest discover -s tests -p "test_node_agent_integration.py" -v
```

Test tích hợp sử dụng `control-plane/tests/agent_e2e_server.py` để khởi động API test riêng, không tác động database PostgreSQL local.

---

## 7. Chạy với Control Plane trên EC2 / RDS

Node Agent và Control Plane được triển khai thành hai tiến trình độc lập:

- Trên EC2 quản lý, Control Plane được chạy bằng Docker Compose, kết nối PostgreSQL RDS.
- Trên Spot VM, Node Agent chạy chương trình `agent.py`, lấy địa chỉ Control Plane và job token từ quá trình cấp phát cấu hình an toàn.
- Agent gọi API bằng **HTTPS** và gửi `X-Agent-Token` trong các request.

Sau khi mã nguồn đã có trên EC2 quản lý, cập nhật API và khởi tạo bảng:

```bash
cd ~/spulti-cloud/control-plane
docker compose up -d --build api
docker compose exec api python -m app.init_db
docker compose ps api
```

Ví dụ lệnh khởi động trên Spot VM (khi VM đã có Python và mã nguồn Node Agent):

```bash
cd ~/spulti-cloud/node-agent
export SPULTI_AGENT_TOKEN='AGENT_TOKEN_DUOC_CAP'
python3 agent.py \
  --job-id 'JOB_ID_DUOC_CAP' \
  --total-steps 100 \
  --checkpoint-every 10 \
  --heartbeat-every 7 \
  --control-plane-url 'https://YOUR_CONTROL_PLANE_DOMAIN'
```

Địa chỉ HTTPS trên chỉ là **placeholder**. Triển khai thực tế cần domain/TLS hợp lệ và cơ chế cấp token an toàn; không đưa token vào startup log hoặc repository.

---

## 8. Các lệnh kiểm tra và lưu ý vận hành

| Hiện tượng | Nguyên nhân thường gặp / cách kiểm tra |
|---|---|
| `service "api" is not running` | API chưa chạy hoặc bị crash; xem `docker compose ps -a` và `logs api` |
| `Table 'agent_jobs' is already defined` | Một bảng đang được định nghĩa nhiều lần trong cùng SQLAlchemy metadata; kiểm tra model/import |
| `ModuleNotFoundError` khi start API | Kiểm tra các import và file trong `control-plane/app/` |
| `relation "agent_jobs" does not exist` | Chạy `python -m app.init_db` với kết nối database đang sử dụng |
| `404 /api/agent/jobs` | Kiểm tra `main.py` đăng ký `agent_router` và image API đã build lại |
| `401 Invalid agent token` | Kiểm tra `job_id`, token và header `X-Agent-Token` |
| `409 Stale checkpoint` | Checkpoint đang upload cũ hơn dữ liệu server |
| `Job config mismatch` | `total_steps` hoặc chu kỳ checkpoint/heartbeat khác cấu hình job |
| `HTTPS` error | Agent không được gửi token qua HTTP đến máy từ xa; dùng HTTPS |

**Giới hạn của phiên bản hiện tại:** Checkpoint mới là JSON cho job đếm bước, không phải snapshot bộ nhớ hay model AI lớn. Hệ thống chưa tự phát hiện heartbeat quá hạn để tạo máy thay thế, chưa tích hợp đầy đủ quy trình bootstrap Agent và chưa lưu checkpoint lớn lên S3/GCS. Những chức năng này thuộc giai đoạn triển khai tiếp theo của Spulti-cloud.
