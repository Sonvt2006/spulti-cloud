# Spulti-cloud Node Agent — MVP 1 (local checkpoint/resume)

> **Mục tiêu của phần này:** Chạy một job giả lập, ghi heartbeat ra file, lưu checkpoint định kỳ, mô phỏng mất máy đột ngột và tiếp tục công việc từ checkpoint. **Chưa** gọi Control Plane, chưa upload lên S3/GCS, chưa chạy job thật, chưa tự phát hiện AWS/GCP interruption.
>
> Độc lập với Docker, RDS, Redis, Celery và cloud credentials. Không tạo VM và không phát sinh chi phí AWS/GCP.

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
    ├── tests/test_recovery.py
    └── README.md
```

Copy thư mục `node-agent/` vào **gốc repository**, ngang hàng với `control-plane/` (không đặt vào bên trong `control-plane/app/`). Cần **Python 3.11+**. Không cần `pip install` cho MVP.

## 2. Chạy trên Windows PowerShell

```powershell
cd "D:\2027 kì 1\Cloud compute\spulti-cloud\node-agent"
python --version
python agent.py --job-id demo-001 --total-steps 1000 --checkpoint-every 100 --heartbeat-every 25 --step-delay 0.01
```

Đợi chạy hết, kết quả có `CHECKPOINT step=100`, `HEARTBEAT ...`, `COMPLETED`.

Trên Ubuntu/Linux (hoặc VM) cú pháp tương tự:

```bash
cd ~/spulti-cloud/node-agent
python3 agent.py --job-id demo-001 --total-steps 1000 --checkpoint-every 100 --heartbeat-every 25 --step-delay 0.01
```

Trên mỗi máy, dữ liệu được lưu ở:

```text
node-agent/state/checkpoints/demo-001.json
node-agent/state/heartbeats/demo-001.json
```

- `last_completed_step`: bước **đã lưu bền vững** để khôi phục.
- `current_step`: bước gần nhất Agent báo heartbeat; **có thể lớn hơn** checkpoint vì heartbeat không phải checkpoint.
- `status`: `RUNNING`, `INTERRUPTED` hoặc `COMPLETED`.
- `timestamp`/`updated_at`: UTC.

Các file JSON được ghi bằng tệp tạm và `os.replace` để tránh checkpoint bị ghi dở.

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

Bốn test kiểm tra: hoàn thành + không chạy lặp, crash và resume, từ chối checkpoint sai `total_steps`, và từ chối checkpoint hỏng (không âm thầm chạy lại từ đầu).

## 5. Những gì đã có / chưa có

| Thành phần | Trạng thái |
| --- | --- |
| Job giả lập CPU counter | MVP hoàn thành |
| Local checkpoint JSON, atomic write | MVP hoàn thành |
| Resume sau khi tiến trình chết đột ngột | MVP hoàn thành |
| Graceful SIGINT/SIGTERM checkpoint | MVP hoàn thành (POSIX; Windows Ctrl+C có thể dùng SIGINT) |
| Local heartbeat JSON | MVP hoàn thành |
| Heartbeat API lên Control Plane | Chưa làm |
| Checkpoint bền vững trên S3/GCS | Chưa làm |
| Tải checkpoint sang VM khác, tự động đổi VM | Chưa làm |
| Lưu checkpoint cho **job thực tế** | Chưa làm; cần workload hỗ trợ snapshot hoặc API checkpoint riêng |

**Giới hạn quan trọng:** Checkpoint đang nằm trên đĩa local của nơi chạy Agent; nếu VM Spot bị hủy, file này cũng có thể mất. Do đó chưa phải cơ chế recovery đa máy. Bước tiếp theo là upload/download checkpoints tới S3/GCS, rồi triển khai Agent và heartbeat API trên VM.

**An toàn:** Không chạy **hai Agent cùng `job-id` và `state-dir` cùng lúc**; MVP chưa có distributed lock. File checkpoint của một job có `total_steps` cố định; đổi tổng bước sẽ báo lỗi để tránh ghi đè sai trạng thái.
