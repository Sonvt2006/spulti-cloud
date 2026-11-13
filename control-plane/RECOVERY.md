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
   python node-agent/package_agent.py
   ```

   Kết quả in đường dẫn `dist/node-agent-<sha256>.tar.gz` và hash; có thêm file
   `.sha256` và manifest `.json` để kiểm tra. Gói chỉ chứa code và requirements,
   không chứa state, credentials hoặc cache; `agent.py` nằm ngay ở gốc archive.
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
   catalog và giá vẫn cần theo [Instance Manager](INSTANCE_MANAGER.md). Dùng Ubuntu
   22.04 / Python 3.10 hoặc Ubuntu 24.04 / Python 3.12 với cập nhật hệ thống mới.
   API phải truy cập được qua HTTPS từ VM; không đưa API key người dùng vào VM.
   Giữ `JWT_SECRET` ổn định giữa API và worker vì nó dẫn xuất token theo attempt.
   Không đổi secret khi còn attempt đã chuẩn bị/đang chạy; cần rollout/reprovision
   có kiểm soát khi xoay khóa.

## Agent thường trực trên mọi máy

Mỗi VM mới, kể cả VM không có `job_id`, tự cài release và chạy
`spulti-node-agent.service` ngay sau bootstrap. Service chạy `agent.py daemon`,
gửi heartbeat mỗi 20 giây, kiểm tra metadata thu hồi AWS/GCP mỗi 2 giây và được
systemd tự khởi động lại nếu tiến trình thoát. Khi managed job hoàn tất, service
tiếp tục giám sát cho tới khi Control Plane xóa VM theo luồng bên dưới.
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

### Bootstrap dừng vì `cannot import name 'UTC'`

Lỗi này xảy ra khi gói S3 cũ dùng `datetime.UTC` trên Python 3.10. Thư mục
`/opt/spulti-agent` đã có code nhưng chưa có `INSTALL_OK` hoặc service. Pull code
control-plane không thay đổi archive trên S3. Đóng gói lại bằng
`python node-agent/package_agent.py`, upload archive mới, rồi cập nhật cả
`SPULTI_AGENT_RELEASE_URL` và `SPULTI_AGENT_RELEASE_SHA256` trong `.env`.
Nên chạy bộ test và lệnh đóng gói bằng Python 3.10 khi dùng Ubuntu 22.04.

Trên EC2 control-plane, sau khi đã cập nhật code, URL và hash:

```sh
cd ~/spulti-cloud/control-plane
docker compose up -d --build --force-recreate api worker beat
umask 077
docker compose exec -T api python -m app.scripts.repair_node_agent --provider AWS > /tmp/repair-spulti-node.sh
```

Chuyển `/tmp/repair-spulti-node.sh` bằng SCP tới **node hiện tại** rồi chạy trên node:

```sh
sudo bash /tmp/repair-spulti-node.sh
sudo systemctl status spulti-node-agent --no-pager -l
sudo journalctl -u spulti-node-agent -n 40 --no-pager
```

Script tải release mới, kiểm tra SHA256, tạo service bằng installer chung và giữ
nguyên credential `/etc/spulti-node.env`; không chạy lại startup script tùy chỉnh.
Chỉ dùng cho attempt hiện tại còn credential hợp lệ. Nếu control-plane đã thay
node lần nữa, dùng node mới; sửa service không kích hoạt lại token đã bị thu hồi.
Với máy không có job, sau khi log có `HEARTBEAT acknowledged`, lần reconcile tiếp
theo sẽ kết thúc recovery. Máy có job cần thêm heartbeat hợp lệ từ job được khôi phục.

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
sử dụng job demo tạo qua `POST /api/agent/jobs`. Không truyền `job_id` thì hệ thống
vẫn cài node-agent và lệnh `spulti-agent` để dùng sau khi SSH. Service job được tạo
nhưng chờ cấu hình job, không tự chạy workload hoặc cấp token job. Máy vẫn có
heartbeat node và recovery VM; khôi phục workload cần đăng ký job/checkpoint.
Mọi máy đều cần API HTTPS, URL HTTPS và SHA256 hợp lệ của gói agent.
Agent đã sửa tương thích Python 3.10 trên Ubuntu 22.04; cần phát
hành lại archive đã sửa và cập nhật URL/hash để VM mới nhận bản này.

Worker cấp credential riêng cho từng attempt trước khi gọi cloud, cài agent,
truyền cấu hình qua file root-only và khởi chạy `agent.py managed` bằng service
`spulti-job-agent`
với user `spulti-agent`. Runner tải source/checkpoint bằng token của job, kiểm tra
checksum và tiếp tục từ bước hoàn thành gần nhất. `startup_script` tùy chọn chạy
trong subshell sau khi node monitor đã chạy, trước khi bắt đầu job, để chuẩn bị
môi trường và dependency. Script lỗi không ngăn heartbeat node.

## Trạng thái và tính an toàn khi retry

### Tự xóa instance khi job hoàn thành

Máy gắn `job_id` tự được xóa khi job báo `COMPLETED` và checkpoint cuối đã lưu
thành công đủ `total_steps`. Cơ chế chạy trong Control Plane, dùng được với
Ubuntu 24.04 / Python 3.12 và không cần cấp quyền xóa cloud cho node-agent.
Không cần chờ heartbeat cuối: nếu VM mất mạng sau khi upload checkpoint thành
công, worker vẫn nhận ra job đã hoàn thành.

Beat phát hiện completion mỗi 15 giây, worker lưu `delete_requested=true` vào
database, hủy recovery đang chờ, rồi dùng luồng xóa hiện có để terminate AWS EC2
hoặc delete GCP VM. Chỉ chuyển máy sang `DELETED` và chốt `ended_at` khi cloud
xác nhận VM đã xóa; không tạo VM thay thế cho job này. Source, checkpoint và lịch
sử job/instance vẫn được giữ. File chỉ nằm trên ổ VM sẽ mất khi ổ bị xóa; kết quả
cần giữ phải được upload/checkpoint trước khi job hoàn thành.

Lỗi xóa tạm thời được thử lại với backoff. Sau 5 lần lỗi, máy hiện
`needs_attention`; sửa lỗi cloud rồi gọi `POST /api/machines/{id}/retry`.
Job lỗi, bị ngắt hoặc upload checkpoint cuối thất bại không kích hoạt tự xóa.
Máy không gắn `job_id`, bao gồm job độc lập chạy bằng `spulti-agent submit` qua
SSH, không thuộc luồng tự xóa này.

Triển khai bằng cách cập nhật API, worker và Beat cùng phiên bản, rồi chạy trên
máy Control Plane từ thư mục `control-plane`:

```sh
docker compose up -d --build --force-recreate api worker beat
```

Thay đổi này không cần migration hoặc phát hành lại node-agent. Nó cũng áp dụng
cho máy hiện có đang gắn job đã hoàn thành và có checkpoint cuối hợp lệ.

### Recovery và retry

- Trigger: VM mất/dừng, báo thu hồi AWS/GCP, agent báo dừng, quá hạn heartbeat
  (90 giây), hoặc không nhận heartbeat đầu tiên sau boot grace (600 giây).
- Node gửi heartbeat mỗi 20 giây, độc lập với job. Managed job đã `COMPLETED`
  và lưu checkpoint cuối sẽ chuyển sang xóa máy, ưu tiên trước recovery.
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
