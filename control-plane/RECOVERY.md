# Lifecycle & Recovery

Recovery chạy trong `spulti.sync_machine`, được Beat đánh thức mỗi 15 giây. Không
cần thêm một worker/service riêng. Các VM quản lý bởi Instance Manager tự được
thay thế khi mất hoặc dừng; job gắn với máy được khôi phục trên VM thay thế.

## Cấu hình và cập nhật

1. Cập nhật API, worker, Beat **cùng phiên bản** và chạy `python -m app.init_db`
   trước khi nhận yêu cầu. Các bảng `machine_jobs`, `recovery_runs`, `node_agents` được tạo
   bổ sung; không ALTER hay xóa dữ liệu các bảng cũ.
2. Làm [hướng dẫn S3 và phát hành agent](S3_SETUP.md) để tạo một bucket dùng chung, cấp quyền
   checkpoint cho API, upload release và cấp URL HTTPS bền vững.
   Đóng gói toàn bộ file Python ở gốc `node-agent/` (bao gồm `managed.py` và `node_daemon.py`) thành
   archive có `agent.py` ở gốc. Ví dụ từ thư mục gốc repo:

   ```sh
   tar -czf /tmp/node-agent-recovery.tar.gz --exclude=tests --exclude=__pycache__ --exclude='*.pyc' --exclude=.venv --exclude='.env*' --exclude='.state*' -C node-agent .
   sha256sum /tmp/node-agent-recovery.tar.gz
   ```

   Đưa archive lên URL HTTPS bất biến do operator quản lý, có thể tải từ AWS/GCP.
   Không dùng presigned URL hết hạn nhanh: VM có thể được cấp sau khi chờ capacity.
3. Cấu hình `.env` / `.env.local`:

   ```dotenv
   SPULTI_PUBLIC_API_URL=https://api.example.com
   SPULTI_AGENT_RELEASE_URL=https://releases.example.com/node-agent-recovery.tar.gz
   SPULTI_AGENT_RELEASE_SHA256=<sha256-64-ky-tu>
   SPULTI_HEARTBEAT_TIMEOUT_SECONDS=90
   SPULTI_AGENT_BOOT_GRACE_SECONDS=600
   SPULTI_RECOVERY_COOLDOWN_SECONDS=300
   SPULTI_RECOVERY_MAX_PER_HOUR=5
   ```

   `SPULTI_CHECKPOINT_BUCKET` và `AWS_REGION` phải trỏ tới bucket dùng chung;
   kiểm tra đọc/ghi từ API theo [S3 setup](S3_SETUP.md). Image/network allowlist,
   catalog và giá vẫn cần theo [Instance Manager](INSTANCE_MANAGER.md). Dùng image Ubuntu 24.04 / Python 3.12 cho installer hiện tại.
   API phải truy cập được qua HTTPS từ VM; không đưa API key người dùng vào VM.
   Giữ `JWT_SECRET` ổn định giữa API và worker vì nó dẫn xuất token theo attempt.
   Không đổi secret khi còn attempt đã chuẩn bị/đang chạy; cần rollout/reprovision
   có kiểm soát khi xoay khóa.

## Agent thường trực trên mọi máy

Mỗi VM mới, kể cả VM không có `job_id`, tự cài release và chạy
`spulti-node-agent.service` ngay sau bootstrap. Service chạy `agent.py daemon`,
gửi heartbeat mỗi 20 giây, kiểm tra metadata thu hồi AWS/GCP mỗi 2 giây và được
systemd tự khởi động lại nếu tiến trình thoát. Job hoàn tất không dừng service này.
Cả ba biến `SPULTI_PUBLIC_API_URL`, `SPULTI_AGENT_RELEASE_URL` và
`SPULTI_AGENT_RELEASE_SHA256` là bắt buộc cho mọi yêu cầu tạo máy.

Credential giám sát riêng cho từng attempt nằm trong `/etc/spulti-node.env`
(root-only). Agent gọi `POST /api/agent/nodes/{request_id}/heartbeat` và
`POST /api/agent/nodes/{request_id}/interruptions` bằng `X-Agent-Token`.
Token này không truy cập được API job; token attempt cũ bị thu hồi khi recovery
bắt đầu. API dùng giờ nhận heartbeat trên server, không tin đồng hồ của VM.

Khi nhận cảnh báo Spot, API lưu ngay vào database và đánh thức worker. Recovery
chờ ít nhất 20 giây trước khi thu hồi credential/xóa máy để job có ngân sách
checkpoint cuối. Báo trùng cùng sự kiện không kéo dài thời gian này. Lỗi Redis
không làm mất cảnh báo; Beat đọc lại intent từ database. Nếu không nhận được
cảnh báo, timeout heartbeat hoặc trạng thái cloud vẫn kích hoạt recovery.
Đây không phải cam kết không có downtime: worker xác nhận VM cũ đã mất trước khi
cấp VM thay thế, và thời gian cấp máy phụ thuộc capacity/cloud.

Kiểm tra sau khi SSH:

```sh
sudo systemctl status spulti-node-agent --no-pager
sudo journalctl -u spulti-node-agent -n 50 --no-pager
```

Log bình thường có `[node] HEARTBEAT acknowledged`. Nếu có managed job, xem riêng
`spulti-job-agent.service`. Dừng/restart service giám sát không bị báo giả thành
cảnh báo Spot; nếu để service dừng quá timeout thì control-plane vẫn thay máy.

Máy/attempt được tạo từ phiên bản cũ không tự được sửa bootstrap. Cập nhật control
plane và release trước khi tạo máy mới. Không bật timeout node cho attempt cũ
chưa được cấp credential node.

Trên local, sau khi điền URL/hash thật và phát hành release mới, chạy từ `control-plane`:

```sh
docker compose --env-file .env.local -f compose.local.yaml build api
docker compose --env-file .env.local -f compose.local.yaml run --rm --no-deps api python -m app.init_db
docker compose --env-file .env.local -f compose.local.yaml up -d api worker beat
```

## Tạo máy chạy job tự động

Đăng ký chương trình qua `PUT /api/agent/submissions/{uuid}` với Bearer token của
người dùng, `filename`, `source_code`, `agent_token`, `total_steps`,
`checkpoint_every`, `heartbeat_every`, `step_delay` như API submission hiện có.
Đây chỉ là đăng ký; chưa chạy code trên control plane.

Sau đó gọi `POST /api/machines`:

```json
{
  "name": "training",
  "vcpu": 4,
  "ram_gib": 16,
  "region": "singapore",
  "optimization_mode": "BALANCED",
  "job_id": "<uuid-job-da-dang-ky>"
}
```

Job phải thuộc cùng người dùng, chưa hoàn thành và chưa gắn với máy khác. Có thể
sử dụng job demo tạo qua `POST /api/agent/jobs`. Không truyền `job_id` thì vẫn có
heartbeat và recovery VM; không có job/checkpoint để tự khôi phục workload.

Worker cấp credential riêng cho từng attempt trước khi gọi cloud, cài agent,
truyền cấu hình qua file root-only và khởi chạy `agent.py managed` bằng service
`spulti-job-agent`
với user `spulti-agent`. Runner tải source/checkpoint bằng token của job, kiểm tra
checksum và tiếp tục từ bước hoàn thành gần nhất. `startup_script` tùy chọn chạy
trong subshell sau khi node monitor đã chạy, trước khi bắt đầu job, để chuẩn bị
môi trường và dependency. Script lỗi không ngăn heartbeat node.

## Trạng thái và tính an toàn khi retry

- Trigger: VM mất/dừng, báo thu hồi AWS/GCP, agent báo dừng, quá hạn heartbeat
  (90 giây), hoặc không nhận heartbeat đầu tiên sau boot grace (600 giây).
- Node gửi heartbeat mỗi 20 giây, độc lập với job. Job đã `COMPLETED` không
  kích hoạt recovery do thiếu heartbeat **job**, nhưng máy vẫn được giám sát.
- Timeout node có lý do `NODE_HEARTBEAT_TIMEOUT` hoặc `NODE_BOOT_TIMEOUT`.
- Recovery có trạng thái `CLEANUP → PLACEMENT → STARTING → SUCCEEDED`.
  Mỗi failed attempt chỉ có một bản ghi nhờ unique constraint và khóa Machine.
- Khi bắt đầu recovery, vô hiệu hóa token cũ. Xóa VM cũ và xác nhận VM đã mất trước
  khi tạo VM mới để tránh hai VM cùng thực thi job. Không coi trạng thái
  `TERMINATED` của GCP là đã xóa; GCP VM phải thực sự không còn tồn tại.
- Xếp hạng lại theo giá/rủi ro hiện tại và yêu cầu gốc; tạm loại trừ
  `(provider, region, zone, machine type)` vừa lỗi. Có thể chuyển AWS ↔ GCP nếu
  yêu cầu cho phép. Nếu chỉ còn đúng nhóm bị loại trừ, đợi cooldown rồi tự thử lại.
- Từ chối do capacity/quota chuyển sang ứng viên khác. Không có ứng viên thì lưu
  lỗi và thử lại sau 30 giây, không cần thao tác thủ công.
- Khóa máy, marker `CREATING` và credential/attempt binding bền vững bảo đảm
  worker chết hoặc Redis mất dữ liệu không dẫn tới một lần create mới không rõ
  nguồn gốc. Retry cloud dùng đúng UUID và startup script đã lưu.
- VM mới `RUNNING` chưa đủ: phải nhận heartbeat node của attempt mới thì recovery
  mới `SUCCEEDED`. Nếu có managed job, phải có cả heartbeat job của attempt mới.
- Xóa máy ưu tiên hơn recovery; trạng thái history chuyển `CANCELLED` và không cấp
  VM thay thế. Lỗi cleanup/network lặp lại hoặc quá số recovery/giờ sẽ bật
  `needs_attention`; sửa nguyên nhân rồi gọi endpoint `/retry` hiện có.
- Giữ source và checkpoint của cùng job qua các attempt. API ghi heartbeat,
  checkpoint và interruption được đồng bộ với thao tác thay token; token cũ
  không được phép cập nhật tiến độ của attempt mới.

`GET /api/machines/{id}` trả thêm `job_id`, `node_last_heartbeat_at` và
`node_status` (`STARTING`, `ONLINE`, `STALE`, `INTERRUPTING`; null cho attempt cũ).
`GET /api/machines/{id}/events?limit=50&offset=0` trả lịch sử recovery, lý do,
failed/replacement attempt, thời điểm, cooldown, lỗi và downtime của lần thành công.
API kiểm tra quyền sở hữu như các endpoint máy khác. Lịch sử instance vẫn nằm tại
`GET /api/machines/{id}/instances`.

## Giới hạn hiện tại

- Khôi phục Python submission theo giao thức `spulti.state` / JSON checkpoint
  hiện có (state 8 KiB, checkpoint 16 KiB), hoặc job demo. Chưa đồng bộ thư mục
  checkpoint ML, dataset, file model lớn hay chạy lệnh shell tùy ý bằng runner.
- Mã của một bước có thể chạy lại sau interruption; side effect bên ngoài cần
  idempotent. Chỉ tiến độ đã checkpoint thành công mới được bảo toàn.
- Mất heartbeat là dấu hiệu cần phục hồi, không phải bằng chứng cloud thu hồi.
  History phân biệt `HEARTBEAT_TIMEOUT`, `BOOT_TIMEOUT`, `INSTANCE_LOST`,
  `INSTANCE_STOPPED`, `SPOT_NOTICE`, `AGENT_STOPPED` để không tạo thống kê thu hồi giả.
- User data chứa credential của attempt (không phải API key người dùng), và snapshot
  của nó nằm trong DB. Hạn chế quyền đọc DB/user-data, không log toàn bộ spec.
- Các máy tạo bằng script cũ không có MachineLifecycle không tự được nhập vào hệ
  thống. Luồng này chưa triển khai/kiểm chứng trên tài khoản cloud thật.

## Kiểm thử

```sh
PYTHONPATH=control-plane .venv/bin/python -m unittest discover -s control-plane/tests -v
.venv/bin/python -m unittest discover -s node-agent/tests -v
```

Đặt `TEST_POSTGRES_URL` tới PostgreSQL test để chạy test khóa đồng thời và worker
crash. Test tạo schema riêng rồi xóa sau mỗi case. Test HTTP dùng server localhost,
checkpoint store giả lập; test cloud dùng fake adapter, không tạo VM có tính phí.
