# Phát hiện thu hồi Spot VM

Task này gồm đọc cảnh báo, dừng workload demo tại bước đã hoàn thành, lưu checkpoint cuối và báo về Control Plane. Không tạo/xóa VM hoặc lập lịch chạy lại job.

## Bật trên VM

Nếu user SSH rồi gửi file Python, dùng [luồng submit/resume](SSH_JOBS.md). Luồng đó tự đăng ký job/token và không cần `/etc/spulti-agent.env`; cấu hình service dưới đây dành cho job được cấp trước.

Node Agent vẫn chỉ cần Python 3.11+ và thư viện chuẩn. Chạy cùng các tham số job đã được API cấp và thêm `--cloud-provider aws` hoặc `--cloud-provider gcp`. Có thể đặt `SPULTI_CLOUD_PROVIDER=aws|gcp` trong môi trường của **Node Agent trên VM**; đặt trong `.env` của Control Plane không tự truyền sang VM.

Ví dụ trên Linux, sau khi đã cấp `SPULTI_AGENT_TOKEN` trong môi trường:

```bash
python3 agent.py --job-id "$SPULTI_JOB_ID" \
  --total-steps 100 --checkpoint-every 0 --heartbeat-every 7 \
  --control-plane-url https://YOUR_CONTROL_PLANE_DOMAIN \
  --cloud-provider aws --step-delay 5 --state-dir /var/lib/spulti-agent
```

Các tham số bước phải khớp job trên API. Mặc định `cloud-provider=none` dành cho local, không truy cập metadata. Hai mẫu systemd trong `control-plane/app/scripts/` đã đặt provider tương ứng và `RestartPreventExitStatus=130`. Với service đã cài trước đó, cập nhật unit tương ứng và chạy `systemctl daemon-reload`. Phần tải release/cấp job của Instance Manager vẫn là luồng triển khai riêng; phải phát hành lại gói agent có cả `interruption.py` trước khi dùng trên VM.

| Tham số | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `--cloud-provider` | `none` | `aws`, `gcp` hoặc tắt polling với `none` |
| `--interruption-poll-interval` | `2` giây | Chu kỳ kiểm tra, cho phép 0.1–5 giây |
| `--shutdown-timeout` | `15` giây | Ngân sách xử lý sau khi phát hiện; cho phép 3–120 giây |
| `--checkpoint-every` | `0` | N > 0 bật thêm lưu mỗi N bước; 0 tắt điều kiện theo bước |
| `--checkpoint-seconds` | `10` giây | Lưu bước/kết quả hoàn thành gần nhất theo thời gian, cả khi chưa có cảnh báo |

AWS đọc `spot/instance-action` qua IMDSv2: cache token, cấp lại sau hết hạn/401, coi 404 là không có cảnh báo; xử lý `stop`, `terminate`, `hibernate`. Thời gian AWS cung cấp sẽ giới hạn thêm ngân sách xử lý. GCP đọc `instance/preempted` với `Metadata-Flavor: Google`, chỉ coi `TRUE` là thu hồi. Request metadata không dùng proxy môi trường và không theo redirect. Lỗi mạng/JSON/5xx chỉ được ghi cảnh báo có giới hạn tần suất và thử lại, không bị nhầm thành thu hồi.

Thread theo dõi đánh thức vòng chạy ngay khi có cảnh báo; bước đang chờ chưa được tính là hoàn thành. `SIGTERM`/`SIGINT` cũng vào cùng luồng lưu cuối, với nguồn `signal` để phân biệt với cảnh báo cloud. Cảnh báo lặp không kéo dài ngân sách hay chạy lại quá trình lưu cuối.

Giới hạn timeout/retry dành khoảng 4 giây cuối để gửi báo cáo độc lập với upload checkpoint. Nếu cảnh báo tới rất muộn, agent ưu tiên một lần báo cáo ngắn sau khi lưu local. HTTP đang chờ phản hồi có thể cần tới socket timeout để thoát; đây không phải cam kết thời gian thực cứng.

## Cập nhật Control Plane

API mới: `POST /api/agent/jobs/{job_id}/interruptions`, xác thực bằng `X-Agent-Token` hiện có. Nó lưu vào bảng mới `agent_interruptions`, độc lập với S3, rồi cập nhật job sang `INTERRUPTED` nếu job chưa hoàn thành và báo cáo không cũ hơn tiến độ hiện có. Endpoint không khởi chạy recovery.

Sau khi cập nhật code API, tạo bảng còn thiếu bằng lệnh hiện có, từ thư mục `control-plane` với đúng môi trường database:

```bash
python -m app.init_db
```

Hoặc khi triển khai bằng Docker Compose:

```bash
docker compose exec api python -m app.init_db
```

Không cần sửa cột của bảng `agent_jobs`. Nên cập nhật API và tạo bảng trước khi phát hành agent mới. `GET /api/agent/jobs/{job_id}` của chủ job có thêm `latest_interruption`:

```json
{
  "event_id": "91773ba6-e7ee-4e92-978b-c00540563ac7",
  "provider": "aws",
  "action": "terminate",
  "detected_at": "2026-11-07T10:00:00Z",
  "deadline_at": "2026-11-07T10:02:00Z",
  "current_step": 37,
  "last_checkpoint_step": 30,
  "local_checkpoint_step": 37,
  "checkpoint_status": "failed",
  "checkpoint_error": "upload_failed",
  "received_at": "2026-11-07T10:00:10Z"
}
```

Trong ví dụ này, local đã lưu bước 37 nhưng nền tảng mới xác nhận bước 30 từ lần chạy trước hoặc khi đã bật lưu định kỳ. Khi upload thành công, `checkpoint_status=uploaded` và `checkpoint_error=null`. Lỗi ghi đĩa là `local_save_failed`. Retry cùng `event_id` và cùng payload được ACK, không tạo thêm bản ghi và không hạ trạng thái job đã hoàn thành. Dùng lại ID với payload khác bị từ chối.

Agent cũng ghi `<state-dir>/interruptions/<job-id>.json`, gồm `reported` cho biết API đã ACK hay chưa. Chế độ local dùng `checkpoint_status=local_only`. Nếu cả mạng và đĩa đều lỗi, chỉ còn thông báo trên stdout; không thể bảo đảm nền tảng nhận báo cáo. Agent thoát mã 130 sau xử lý; service phải tránh restart mã này kể cả khi upload lỗi. File local không phải hàng đợi tự gửi lại sau reboot.

## Kiểm thử

Từ thư mục tương ứng với Python môi trường dự án:

```bash
# node-agent/
python -m unittest discover -s tests -v
# control-plane/
python -m unittest discover -s tests -p test_node_agent_integration.py -v
```

Unit test giả lập metadata và lỗi mạng/đĩa. Integration test chạy agent, FastAPI và SQLite thật; kho checkpoint được thay bằng fixture trong bộ nhớ để không dùng S3 hay credentials thật. Nó kiểm tra lưu cuối thành công, upload thất bại vẫn báo được, xác thực, validation và retry sau khi job đã hoàn thành.

## Giới hạn và tài liệu provider

AWS phát cảnh báo best-effort, thông thường trước stop/terminate hai phút; hibernate có thể bắt đầu ngay. Xem [AWS Spot interruption notices](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-instance-termination-notices.html) và [IMDSv2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html).

GCP có thời gian shutdown best-effort và có cấu hình thời gian báo trước tùy VM; cờ `preempted` không cung cấp deadline để agent suy ra chính xác thời gian còn lại. Polling và tín hiệu dừng đều là best-effort, không bảo đảm bắt được trước khi OS dừng tiến trình. Xem [GCP: xử lý và phát hiện preemption](https://docs.cloud.google.com/compute/docs/instances/create-use-spot#handle_preemption).

Mặc định lưu mỗi 10 giây và khi dừng/thu hồi/hoàn thành. Nếu VM mất đột ngột mà agent không kịp xử lý cảnh báo, VM mới khôi phục từ checkpoint đã đồng bộ thành công gần nhất; công việc sau bản đó phải chạy lại. Snapshot chỉ chứa bước đã hoàn thành, không chứa trạng thái đang chạy dở. Chu kỳ theo thời gian không phải cam kết mất tối đa 10 giây khi có bước dài hoặc lỗi mạng/API. Checkpoint version 2 lưu bước và `workload_state.running_sum` của demo; script gửi lên dùng version 3 và dữ liệu `spulti.state`. Một job hiện giả định chỉ có một agent hoạt động; cơ chế phân biệt các lần chạy/recovery và tạo VM thay thế thuộc task khác.
