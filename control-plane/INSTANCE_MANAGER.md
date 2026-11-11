# Instance Manager

Instance Manager nối API quản lý máy, Placement Engine, Celery và các adapter
AWS/GCP. `Machine` là máy logic của người dùng; `Instance` là VM cụ thể trên cloud.
Tên/ID máy logic giữ nguyên khi cấp VM thay thế, còn lịch sử instance được giữ lại.

## Chức năng

- API tạo, liệt kê, xem, xóa, retry và xem lịch sử instance; xác thực JWT/API key,
  kiểm tra chủ sở hữu trên mọi endpoint.
- Hạn mức số máy chưa xóa của mỗi tài khoản, mặc định 5; khóa khi kiểm tra để hai
  yêu cầu đồng thời không vượt hạn mức.
- Lưu cấu hình tạo và SSH public key trước khi gọi cloud; dùng lại chính xác
  cấu hình này khi retry cùng một lần cấp máy.
- Chọn ứng viên bằng Placement Engine; chỉ dùng image/network do quản trị viên
  cấu hình. Thử ứng viên tiếp theo khi có lỗi capacity/quota được nhận diện rõ.
- Theo dõi trạng thái/IP, xác nhận VM đã kết thúc trước khi đánh dấu xóa xong.
- Tiếp tục xử lý khi Redis hoặc worker restart bằng dữ liệu trong PostgreSQL.

## Luồng xử lý

```mermaid
sequenceDiagram
    participant U as Người dùng
    participant A as FastAPI
    participant D as PostgreSQL
    participant W as Celery worker
    participant C as AWS/GCP
    U->>A: POST /api/machines + Idempotency-Key
    A->>D: Kiểm tra tài khoản, SSH key, hạn mức; xếp hạng ứng viên
    A->>D: Lưu Machine, lifecycle và các attempt
    A-->>U: 202 + ID máy + PENDING
    A->>W: Gửi tín hiệu xử lý
    W->>D: Chuẩn bị máy PROVISIONING
    Note over D,W: Beat gửi máy đến hạn mỗi 15 giây
    W->>D: Khóa Machine; lưu bền attempt CREATING trước khi tạo
    W->>C: Tìm lại VM nếu retry; tạo nếu chưa thấy
    C-->>W: ID, ref, trạng thái, IP
    W->>D: Lưu Instance và trạng thái thực tế
    U->>A: GET /api/machines/{id}
    A-->>U: Trạng thái gần nhất đã đồng bộ
```

`202` xác nhận yêu cầu đã được lưu, chưa bảo đảm VM đã chạy. GET đọc database,
không gọi cloud; `checked_at` cho biết lần đồng bộ gần nhất. `RUNNING` nghĩa là
cloud báo VM đang chạy, chưa chứng minh SSH/cloud-init/job đã sẵn sàng.

Beat chạy mỗi 15 giây, gửi tối đa 500 máy đến hạn. Sau khi đồng bộ thành công,
máy được hẹn kiểm tra tiếp sau 30 giây. Độ trễ thực tế còn phụ thuộc hàng đợi và
cloud. Cần chạy cả worker lẫn **một** tiến trình beat.

## Trạng thái và dữ liệu

Giữ nguyên các trạng thái đã có trong schema `machines`:

| Trạng thái | Ý nghĩa |
| --- | --- |
| `PENDING` | Đã lưu yêu cầu, chờ bắt đầu cấp máy. |
| `PROVISIONING` | Đang tạo/xác minh kết quả tạo hoặc VM đang khởi động. |
| `RUNNING` | Cloud báo VM đang chạy. |
| `RECOVERING` | VM mất hoặc không còn chạy; đang dọn/thay VM và khôi phục job. |
| `DELETED` | Chưa gửi tạo VM, hoặc đã xác nhận VM quản lý kết thúc/không còn tồn tại. |

`delete_requested=true` thể hiện ý định xóa trong lúc trạng thái máy vẫn phản ánh
lần quan sát trước. Không bổ sung `DELETING` vào constraint cũ.
`needs_attention=true` và `last_error` thể hiện lỗi cần xử lý. Máy lỗi vẫn tính
vào hạn mức cho đến khi xóa xong.

AWS `terminated` là kết thúc instance. GCP `TERMINATED` là VM đã dừng nhưng vẫn
tồn tại; chỉ xác nhận xóa GCP khi truy vấn trả `NotFound`.
Xem [vòng đời VM GCP](https://docs.cloud.google.com/compute/docs/instances/instance-lifecycle).

Thêm hai bảng trong `app/models.py`, không sửa cột/constraint của bảng cũ:

| Bảng | Nội dung |
| --- | --- |
| `machine_lifecycles` | Yêu cầu gốc, khóa idempotency, ý định xóa, lịch đồng bộ, lỗi và số lần thất bại. |
| `instance_attempts` | Ứng viên theo thứ tự, UUID riêng, snapshot `CreateInstanceSpec`, cấu hình adapter, liên kết instance, ref và IP. |

Attempt đi qua `READY → CREATING → ACTIVE → ENDED`. Cloud từ chối capacity/quota
thì attempt chuyển `REJECTED`; khi hết ứng viên, máy cần retry thủ công.
`Instance.started_at`/`ended_at` là thời điểm control plane ghi nhận, chưa phải
thời điểm tính phí chính xác của nhà cung cấp.

## Chống tạo trùng và xử lý đồng thời

1. **HTTP `Idempotency-Key`** là UUID client giữ khi gửi lại POST. Cùng tài khoản,
   key và payload trả lại máy cũ; cùng key nhưng payload khác trả `409`. Bỏ header
   thì server sinh key và trả trong `request_key`; để retry khi mất phản hồi POST
   đầu tiên, client cần tự sinh và giữ key từ trước.
2. **UUID của attempt** gửi tới AWS `ClientToken` hoặc GCP `request_id`. UUID cùng
   SSH key, image, zone, loại máy, disk, script và network được lưu trước khi gọi
   cloud. Đổi SSH key/cấu hình môi trường không đổi attempt đang retry.

Tham khảo [idempotency AWS](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-idempotency.html)
và [requestId GCP](https://cloud.google.com/compute/docs/reference/rest/v1/instances/insert).
Manager còn tìm VM bằng client token AWS hoặc tên và label machine/request GCP.
Gặp cùng tên nhưng khác request/ID thực tế thì dừng, tránh nhận nhầm/xóa VM khác.

Worker giữ khóa hàng `Machine` khi gọi cloud; worker nhận task trùng dùng
`FOR UPDATE SKIP LOCKED` để bỏ qua. Ý định xóa nằm ở bảng riêng nên API vẫn ghi
nhận được khi tạo đang chạy. Worker đọc lại ý định sau cloud call và dọn VM vừa
tạo nếu đã có yêu cầu xóa. PostgreSQL là môi trường chạy được hỗ trợ; SQLite chỉ
dùng cho kiểm thử đơn luồng.

Marker `CREATING` được commit bằng một kết nối riêng ngay trước khi gọi create,
trong lúc kết nối chính vẫn giữ khóa Machine. Worker có thể dùng hai kết nối DB
trong bước này; pool cần đủ cho số worker đồng thời. Hủy máy đang chờ trong
`READY` không gọi cloud và không rơi vào tình trạng kết quả tạo chưa rõ.

## Cấu hình và khởi chạy

1. Cấu hình credentials cho worker theo [README.local.md](README.local.md) hoặc
   [README.md](README.md).
   Nếu chạy job có checkpoint/recovery, làm [S3 setup](S3_SETUP.md) để cấp quyền
   S3 cho **API**, cấu hình bucket/region và phát hành Node Agent qua HTTPS.
   Credentials cấp VM cho worker không tự chứng minh API đọc/ghi được checkpoint.
2. Nạp catalog, offerings và giá spot. Seed minh họa không đủ để cấp máy thật;
   kiểm tra trước bằng `POST /api/placement/recommendations`.
3. Đặt `SPULTI_LAUNCH_CONFIG` trong `.env.local` hoặc `.env`. Đây là JSON array;
   mỗi profile cho phép những loại máy đã kiểm tra tương thích image.

Ví dụ **phải thay ID placeholder trước khi dùng**:

```dotenv
SPULTI_MAX_MACHINES_PER_USER=5
SPULTI_LAUNCH_CONFIG='[{"provider":"AWS","region":"ap-southeast-1","zone":"ap-southeast-1a","image_id":"ami-REPLACE","machine_types":["m6i.xlarge"],"subnet_id":"subnet-REPLACE","security_group_ids":["sg-REPLACE"]},{"provider":"GCP","region":"asia-southeast1","zone":"asia-southeast1-b","image_id":"projects/PROJECT/global/images/IMAGE","machine_types":["e2-standard-4"],"project_id":"PROJECT","subnetwork":"projects/PROJECT/regions/asia-southeast1/subnetworks/SUBNET"}]'
```

Profile không được trùng provider/region/zone/machine type. AWS subnet phải thuộc
đúng zone, image đúng region/kiến trúc và firewall cho phép kết nối cần thiết.
Ưu tiên image ID/version cố định để retry không phụ thuộc image family thay đổi.
Không đưa credentials vào JSON; SDK tiếp tục lấy credentials từ môi trường/role.

Client không tự truyền project/subnet/security group. `gpu_count` lọc máy có GPU
tích hợp trong catalog; chưa hỗ trợ chọn GPU model hoặc gắn thêm GPU N1.

Từ thư mục `control-plane`, sau khi cấu hình local sẵn sàng:

```bash
docker compose --env-file .env.local -f compose.local.yaml build
docker compose --env-file .env.local -f compose.local.yaml up -d --wait postgres redis
docker compose --env-file .env.local -f compose.local.yaml run --rm --no-deps api python -m app.init_db
docker compose --env-file .env.local -f compose.local.yaml up -d --wait
```

Với EC2:

```bash
docker compose build
docker compose run --rm --no-deps api python -m app.init_db
docker compose up -d --build
```

`init_db` tạo hai bảng mới, giữ dữ liệu cũ. Máy cũ không tự có snapshot lifecycle;
API từ chối xóa máy đó để tránh đoán sai project/ref. VM do script thử adapter tạo
vẫn quản lý bằng script, không tự nhập vào manager.

Ngoài Docker, export biến môi trường rồi mở ba tiến trình riêng:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
celery -A app.celery_app:celery_app worker --loglevel=INFO --concurrency=2
celery -A app.celery_app:celery_app beat --loglevel=INFO --schedule=/tmp/spulti-celerybeat
```

## API sử dụng

Đăng nhập và đăng ký SSH public key qua `/api/ssh-keys` trước. Ví dụ tạo máy local:

```bash
curl -X POST http://localhost:8080/api/machines \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 62525781-740a-480b-98d2-cac4e24b45e0' \
  -d '{"name":"training-01","vcpu":4,"ram_gib":16,"disk_size_gb":30,"region":"singapore","optimization_mode":"BALANCED"}'
```

Giữ key khi gửi lại cùng yêu cầu; dùng UUID mới cho máy mới. Tùy chọn thêm
`provider`, `zone`, `gpu_count`, `startup_script` (phải có shebang). `limit` là số
ứng viên Placement xét, mặc định 20, tối đa 100; allowlist launch được lọc sau khi
xếp hạng. Không có ứng viên được cấu hình trong danh sách này thì trả `409`.

| Endpoint | Kết quả |
| --- | --- |
| `POST /api/machines` | `202`, máy đã lưu và `request_key`. |
| `GET /api/machines?limit=50&offset=0` | Máy của tài khoản, mới nhất trước; gồm cả máy đã xóa. |
| `GET /api/machines/{id}` | Trạng thái, lỗi, IP và instance hiện tại. |
| `GET /api/machines/{id}/instances` | Lịch sử instance do manager quản lý. |
| `DELETE /api/machines/{id}` | `202`, ghi ý định xóa; gọi GET chờ `DELETED`. |
| `POST /api/machines/{id}/retry` | `202`, mở lại xử lý sau khi sửa nguyên nhân lỗi. |

Máy không tồn tại/thuộc người khác trả `404`; chưa đăng nhập trả `401`; dữ liệu/SSH
key không hợp lệ trả `422`; vượt hạn mức/không có ứng viên trả `409`; cấu hình
launch thiếu/sai trả `503`. Redis lỗi sau khi lưu yêu cầu vẫn trả `202`; beat gửi
lại khi Redis hoạt động. Khi VM sẵn sàng, SSH bằng private key tương ứng tới
`spulti@<PUBLIC_IP>`.

## Retry, xóa và giới hạn

- Chỉ chuyển ứng viên khi có lỗi rõ ràng: AWS `InsufficientInstanceCapacity`,
  `InsufficientHostCapacity`, `InsufficientFreeAddressesInSubnet`,
  `MaxSpotInstanceCountExceeded`, `VcpuLimitExceeded`; GCP operation
  `ZONE_RESOURCE_POOL_EXHAUSTED`, `ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS`,
  `QUOTA_EXCEEDED`.
- Timeout/lỗi chưa rõ kết quả giữ nguyên attempt. Retry sau 10, 20, 40, 80 giây
  cộng độ trễ beat/queue; dừng tự động sau 5 lần thất bại. Không chuyển cloud chỉ
  vì mất phản hồi. Chi tiết exception nằm trong log worker.
- Lỗi từ chối rõ ràng ở lần gửi create đầu tiên, như AMI không tồn tại hoặc thiếu
  quyền insert, dừng với `needs_attention=true`; không tự đổi cloud. Có thể xóa
  máy chưa được tạo, hoặc sửa cấu hình/IAM rồi retry với snapshot và UUID mới.
  Lỗi khi polling một operation đã được nhận vẫn giữ nguyên attempt chưa rõ kết quả.
- Xóa khi kết quả tạo chưa rõ: worker chỉ tìm/dọn VM tìm được, không tạo mới.
  Không tìm thấy sau các lần retry sẽ cần kiểm tra thủ công. Trường hợp này cũng
  có thể xảy ra nếu worker dừng sau khi lưu `CREATING` nhưng chưa gửi request;
  database không chứng minh được request đã tới cloud. Giữ snapshot và đối chiếu
  request ID/tên VM trên cloud trước khi xử lý.
- Retry sau timeout dùng snapshot cũ. Hết ứng viên và không còn attempt chưa kết
  thúc thì retry tạo danh sách mới/UUID mới, giữ lịch sử cũ.
- VM mất thì chuyển `RECOVERING`; retry có thể cấp VM thay thế khi VM cũ đã kết
  thúc. VM cũ còn tồn tại nhưng dừng thì retry chỉ kiểm tra VM đó, chưa cấp thêm VM.

**Phạm vi Recovery:** đã có điều phối thay VM, cooldown ứng viên, heartbeat timeout,
bootstrap managed job và khôi phục Python/JSON checkpoint; xem [RECOVERY.md](RECOVERY.md).
Polling không chứng minh nguyên nhân VM mất, nên lịch sử recovery phân biệt mất VM,
heartbeat timeout và thông báo thu hồi. Chưa có cloud quota preflight.

## Kiểm thử

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
```

Mặc định dùng SQLite/cloud giả lập. Để kiểm tra khóa thực tế, đặt URL PostgreSQL
thử nghiệm có quyền tạo schema:

```bash
TEST_POSTGRES_URL='postgresql+psycopg://user:password@localhost:5432/test_db' \
  python -m unittest discover -s tests -v
```

Kiểm thử PostgreSQL tạo schema ngẫu nhiên, kiểm tra request trùng/hạn mức đồng
thời, worker trùng và xóa trong lúc tạo, rồi xóa đúng schema thử nghiệm đó.
Không gọi AWS/GCP thật. Smoke test cloud cần image/network, credentials, IAM và
quota hợp lệ của môi trường triển khai.
