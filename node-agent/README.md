# Spulti-cloud Node Agent — checkpoint/resume và phát hiện thu hồi

**PyTorch (mỗi lần chạy file = một epoch):** xem [PYTORCH.md](PYTORCH.md)
và [ví dụ training CPU](examples/torch_regression.py). Helper `TorchCheckpoint`
lưu/nạp model, optimizer, scheduler và RNG, đồng bộ sau mỗi epoch thành công.

VM do Instance Manager tạo luôn chạy `spulti-node-agent.service` (`agent.py daemon`)
để gửi heartbeat mỗi 20 giây và theo dõi thu hồi, kể cả khi không có job.
Managed job chạy riêng trong `spulti-job-agent.service`; khi job hoàn thành và
checkpoint cuối đã lưu thành công, Control Plane tự xóa instance gắn với job.
Node-agent tiếp tục giám sát cho tới lúc VM bị xóa. Job độc lập submit qua SSH
chưa gắn máy không tự xóa instance. Xem [triển khai và recovery](../control-plane/RECOVERY.md).

**Chạy file Python sau khi SSH:** xem [SSH_JOBS.md](SSH_JOBS.md). Dùng `spulti-agent submit hello.py --total-steps 100` để kiểm tra cú pháp, tự đăng ký job và chạy toàn bộ file 100 lượt. File lỗi bị dừng và báo lỗi. Code cần khôi phục kết quả dùng `spulti.state`; `spulti-agent resume JOB_ID` tải lại code/checkpoint. Các lệnh bên dưới chạy workload demo tổng bình phương có sẵn.

> Tính tổng bình phương `1² + 2² + ... + N²`, mặc định chờ 5 giây trước mỗi bước. Lưu checkpoint mỗi 10 giây và khi dừng bằng Ctrl+C/SIGTERM, nhận cảnh báo thu hồi hoặc hoàn thành job. Checkpoint lưu bước đã hoàn thành gần nhất và tổng tại bước đó. Agent hỗ trợ local/Control Plane và [phát hiện thu hồi AWS/GCP](INTERRUPTION.md).
>
> Demo local không cần Docker, RDS, Redis, Celery hay cloud credentials và không tạo VM. Chế độ remote dùng Control Plane/S3; tài nguyên cloud có thể phát sinh chi phí.

Triển khai cloud cần [S3 và bản phát hành agent](../control-plane/S3_SETUP.md)
trước khi chạy checkpoint/resume remote. Xem [Recovery](../control-plane/RECOVERY.md)
cho luồng tự cài agent và khôi phục job trên VM thay thế.

## 1. Vị trí trong repo

```text
spulti-cloud/
├── control-plane/
└── node-agent/
    ├── agent.py
    ├── config.py
    ├── checkpoint.py
    ├── heartbeat.py
    ├── job_runner.py
    ├── control_plane.py
    ├── interruption.py
    ├── tests/test_recovery.py
    └── README.md
```

Copy thư mục `node-agent/` vào **gốc repository**, ngang hàng với `control-plane/` (không đặt vào bên trong `control-plane/app/`). Cần **Python 3.10+**. Không cần `pip install` cho MVP.

## 2. Chạy trên Windows PowerShell

```powershell
cd D:\spulti-cloud\node-agent
python --version
..\.venv\Scripts\python.exe agent.py --job-id squares-demo --total-steps 10 --heartbeat-every 1
```

Sau mỗi 5 giây, log hiển thị bước, bình phương và tổng: `STEP=3 SQUARE=9 SUM=14`. Bước 10 có tổng `385`. Nhấn Ctrl+C rồi chạy lại cùng lệnh để nạp bước/tổng đã lưu và tiếp tục; job đã hoàn thành sẽ không chạy lại.

Trên Ubuntu/Linux (hoặc VM) cú pháp tương tự:

```bash
cd ~/spulti-cloud/node-agent
python3 agent.py --job-id squares-demo --total-steps 10 --heartbeat-every 1
```

Trên mỗi máy, dữ liệu được lưu ở:

```text
node-agent/state/checkpoints/squares-demo.json
node-agent/state/heartbeats/squares-demo.json
```

- `last_completed_step`: bước **đã lưu bền vững** để khôi phục.
- `workload_state.running_sum`: tổng bình phương ở chính bước đã lưu.
- `current_step`: bước gần nhất Agent báo heartbeat; **có thể lớn hơn** checkpoint vì heartbeat không phải checkpoint.
- `status`: `RUNNING`, `INTERRUPTED` hoặc `COMPLETED`.
- `timestamp`/`updated_at`: UTC.

Các file JSON được ghi bằng tệp tạm và `os.replace` để tránh checkpoint bị ghi dở.

Ví dụ checkpoint khi dừng ở bước 3:

```json
{
  "version": 2,
  "job_id": "squares-demo",
  "total_steps": 10,
  "last_completed_step": 3,
  "status": "INTERRUPTED",
  "updated_at": "2026-11-07T10:00:00+00:00",
  "workload": "sum_of_squares",
  "workload_state": {"running_sum": 14}
}
```

Khi khôi phục, agent nạp `step=3`, `running_sum=14`; sau 5 giây tính bước 4: `14 + 4² = 30`. Công thức tổng bình phương chỉ dùng để kiểm tra checkpoint có khớp bước hay không; vòng lặp thực sự cộng từng `step ** 2` vào tổng được nạp.

Checkpoint đếm bước cũ (`version=1`) không chứa kết quả nên agent mới từ chối khôi phục và không ghi đè. Dùng job-id/thư mục state mới cho demo này; nếu kết nối API thì tạo job mới trên API. Cập nhật API trước agent để API nhận schema version 2. API vẫn nhận version 1 từ agent cũ. Thay đổi schema JSON này không yêu cầu thêm cột database.

Mặc định `--checkpoint-seconds 10` lưu bước/kết quả gần nhất mỗi 10 giây, kể cả khi đang chờ delay hoặc chạy một lượt Python dài. `--checkpoint-every 0` chỉ tắt điều kiện theo số bước; N > 0 bật thêm lưu sau mỗi N bước. Dừng/kết thúc vẫn lưu thêm bản cuối. File checkpoint local của job được thay bằng bản mới. Chế độ remote đồng bộ từng snapshot lên Control Plane/S3; nếu VM mất đột ngột thì khôi phục từ bản đã upload thành công. Chu kỳ này không đảm bảo giới hạn mất công việc đúng 10 giây khi một bước dài hoặc mạng/API lỗi. Có thể đổi `--step-delay 0` để test nhanh.

Khi dùng API, `total_steps`, `checkpoint_every`, `heartbeat_every` phải khớp job; job mới cũng mặc định `checkpoint_every=0`. Job cũ được tạo với chu kỳ khác cần tạo job mới có `checkpoint_every=0` nếu muốn dùng chế độ này. Với systemd, đặt `SPULTI_CHECKPOINT_EVERY=0` trong `/etc/spulti-agent.env` cho khớp job. Các ví dụ crash bên dưới cố ý bật lưu định kỳ mỗi 100 bước.

## 3. Test mất máy đột ngột và khôi phục

**Windows PowerShell** (trong thư mục `node-agent`):

```powershell
# 1. Chạy đến bước 355 rồi giả lập mất máy đột ngột.
python agent.py --job-id recovery-test --total-steps 1000 --checkpoint-every 100 --heartbeat-every 25 --step-delay 0 --simulate-crash-at 355

# 2. Đọc checkpoint: dự kiến last_completed_step = 300.
Get-Content .\state\checkpoints\recovery-test.json

# 3. Chạy lại cùng cấu hình, không cần tham số crash.
python agent.py --job-id recovery-test --total-steps 1000 --checkpoint-every 100 --heartbeat-every 25 --step-delay 0

# 4. Đọc checkpoint cuối cùng: status=COMPLETED, last_completed_step=1000.
Get-Content .\state\checkpoints\recovery-test.json
```

Ubuntu/Bash:

```bash
python3 agent.py --job-id recovery-test --total-steps 1000 --checkpoint-every 100 --heartbeat-every 25 --step-delay 0 --simulate-crash-at 355
cat state/checkpoints/recovery-test.json
python3 agent.py --job-id recovery-test --total-steps 1000 --checkpoint-every 100 --heartbeat-every 25 --step-delay 0
cat state/checkpoints/recovery-test.json
```

**Kỳ vọng:** Lần 1 dừng với exit code `75`, checkpoint còn ở bước `300` dù tiến trình đã làm đến `355`. Lần 2 in `RESUME_FROM=300 NEXT_STEP=301`, rồi chạy đến `1000` và hoàn thành. Đây là mô phỏng crash bằng `os._exit()`, không phải tắt VM thật.

Muốn chạy thử lại từ đầu, chọn **job-id mới** (ví dụ `recovery-test-02`) thay vì xóa dữ liệu checkpoint đang cần kiểm tra. Sau khi `COMPLETED`, chạy lại cùng job-id sẽ báo `already COMPLETED` và không thực hiện lại.

## 4. Chạy bài test tự động

```powershell
python -m unittest discover -s tests -v
```

Hoặc Ubuntu:

```bash
python3 -m unittest discover -s tests -v
```

Các test kiểm tra checkpoint/resume, metadata AWS/GCP, tín hiệu dừng, giới hạn thời gian xử lý, lỗi lưu/upload và báo cáo thu hồi. Tất cả dùng nguồn metadata giả lập, không tạo VM.

## 5. Những gì đã có / chưa có

| Thành phần | Trạng thái |
| --- | --- |
| Job tính tổng bình phương, delay 5 giây | Đã có; lưu/nạp bước và tổng |
| Local checkpoint JSON, atomic write | MVP hoàn thành |
| Resume sau khi tiến trình chết đột ngột | MVP hoàn thành |
| Graceful SIGINT/SIGTERM checkpoint | MVP hoàn thành (POSIX; Windows Ctrl+C có thể dùng SIGINT) |
| Local heartbeat JSON | MVP hoàn thành |
| Heartbeat API lên Control Plane | Đã có khi cấu hình URL/token |
| Checkpoint bền vững trên S3 | Control Plane nhận JSON và lưu S3 |
| Phát hiện thu hồi AWS/GCP, lưu cuối và báo nền tảng | Đã có; xem `INTERRUPTION.md` |
| Tải checkpoint từ Control Plane | Đã có |
| Tự tạo VM thay thế và chạy lại Agent | Task riêng, chưa triển khai |
| Lưu checkpoint cho **job thực tế** | Đã hỗ trợ script Python dùng spulti.state; chưa hỗ trợ tự động chụp toàn bộ tiến trình hoặc khôi phục giữa một lượt chạy |

**Giới hạn:** Khi chạy local không cấu hình URL/token, dữ liệu chỉ nằm trên đĩa VM. Khi kết nối API, chỉ checkpoint đã được nền tảng xác nhận mới được coi là đồng bộ. Với chế độ chỉ lưu lúc dừng/kết thúc, nếu tiến trình bị kill đột ngột hoặc VM mất trước khi kịp xử lý cảnh báo thì tiến độ từ lần lưu gần nhất chưa được ghi lại. Agent hiện chỉ chạy workload demo.

**An toàn:** Không chạy **hai Agent cùng `job-id` và `state-dir` cùng lúc**; MVP chưa có distributed lock. File checkpoint của một job có `total_steps` cố định; đổi tổng bước sẽ báo lỗi để tránh ghi đè sai trạng thái.

### Managed recovery

Instance Manager có thể cấp VM và chạy `agent.py managed` bằng token riêng cho
attempt. Runner tự tải chương trình/checkpoint, không cần API key của người dùng.
Agent gửi thêm heartbeat theo thời gian mỗi 20 giây trong khi chờ hoặc chạy một
bước dài. Xem [Lifecycle & Recovery](../control-plane/RECOVERY.md) để cấu hình
release, gắn job vào máy và kiểm tra lịch sử thay VM.
