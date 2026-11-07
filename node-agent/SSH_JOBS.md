# Gửi và khôi phục job sau khi SSH vào Spot VM

User gửi một file Python cùng số lượt chạy. Agent kiểm tra cú pháp, tự đăng ký `job_id`, lưu code và thực thi **toàn bộ file một lần cho mỗi lượt**. `--total-steps 100` nghĩa là chạy file 100 lần, với Python process mới cho từng lượt. Checkpoint giữ lượt đã hoàn thành và trạng thái JSON do chương trình cung cấp. Khi khôi phục bằng ID, agent tải lại code và checkpoint từ Control Plane.

## Chuẩn bị nền tảng một lần

1. Cập nhật code Control Plane, chạy `python -m app.init_db` từ thư mục `control-plane` với môi trường database đang dùng. Nếu dùng Docker Compose: `docker compose exec api python -m app.init_db`. Lệnh tạo thêm bảng `agent_programs` (và `agent_interruptions` nếu chưa có), không sửa cột của bảng hiện có.
2. Đóng gói/phát hành lại toàn bộ `node-agent/`, bao gồm `submission.py`, `program.py`, `script_worker.py`, `spulti.py`, `interruption.py` và các module hiện có, rồi cài vào `/opt/spulti-agent` trên VM. Các bootstrap hiện vẫn trỏ tới release S3 đã cấu hình; phải cập nhật release/checksum khi triển khai code mới.
3. Hai mẫu bootstrap AWS/GCP đã thêm launcher `/usr/local/bin/spulti-agent` và đặt provider tương ứng. Nếu VM chưa có launcher, dùng `python3 /opt/spulti-agent/agent.py` thay cho `spulti-agent` trong các lệnh dưới đây.

Luồng `submit` chạy bằng tài khoản SSH, không qua systemd service dành cho demo cố định. Nó không cần `/etc/spulti-agent.env` và không cần cấp trước `job_id`/`SPULTI_AGENT_TOKEN`.

## File code của user

File Python thông thường không cần hàm `step()`. Ví dụ `hello.py`:

```python
print("hello spulti")
```

```bash
spulti-agent submit hello.py --total-steps 100
```

Lệnh trên in `hello spulti` 100 lần, mặc định chờ 5 giây trước mỗi lượt. Thêm `--step-delay 0` để chạy liên tiếp. Agent cũng thực thi phần `if __name__ == "__main__":` như khi chạy Python thông thường. Nếu file đã có một vòng `for` 10 lần thì cả vòng `for` ấy sẽ chạy lại trong mỗi lượt, tức tổng cộng 1.000 lần với `--total-steps 100`.

Khi cần giữ kết quả giữa các lượt và khôi phục khi đổi VM, dùng module `spulti` do agent cung cấp. Ví dụ `sum_squares.py`:

```python
from spulti import state, step_number

state["running_sum"] = state.get("running_sum", 0) + step_number**2
print(f"step={step_number} sum={state['running_sum']}")
```

`spulti` là file `node-agent/spulti.py` đi kèm bộ agent, không phải thư viện cần cài từ pip. `script_worker.py` nạp module và gán checkpoint/bước hiện tại trước khi thực thi code của user. Vì vậy hãy chạy ví dụ bằng `spulti-agent submit sum_squares.py --total-steps 100`; chạy trực tiếp bằng `python sum_squares.py` ngoài môi trường agent không có bước/state được khởi tạo này và có thể không tìm thấy module.

`step_number` bắt đầu từ 1; `total_steps` cũng có thể import từ `spulti`. Logic có thể là nhân, xử lý dữ liệu hoặc bất kỳ thuật toán nào; agent không kiểm tra công thức tổng bình phương. Chương trình không muốn import module có thể đọc `SPULTI_STEP` và `SPULTI_TOTAL_STEPS` từ biến môi trường.

`state` là dictionary JSON, giới hạn 8 KiB khi mã hóa: khóa chuỗi, giá trị là chuỗi/số hữu hạn/bool/null/list/object. Hãy ghi kết quả cần giữ vào dictionary này. Biến Python thông thường, stdout và toàn bộ RAM của chương trình không tự trở thành checkpoint. Nếu không dùng `state`, checkpoint vẫn giữ tiến độ và `workload_state` là `{}`. File mẫu nằm trong `examples/hello.py` và `examples/sum_squares.py`.

File tối đa 16 KiB, UTF-8; thư viện và file dữ liệu cần có sẵn trên VM. Agent dùng cùng Python interpreter với tiến trình agent. Nó chạy bản code đã đăng ký trong thư mục tạm, giữ working directory của lệnh CLI; file đi kèm dựa vào `__file__` không được tự upload. Control Plane chỉ lưu/kiểm tra cú pháp, không thực thi code. Code chạy với quyền của tài khoản SSH và không được sandbox riêng.

## Khi file lỗi

Agent gọi `compile()` trước khi đăng ký job, không chạy code trong bước kiểm tra. File lỗi cú pháp bị từ chối toàn bộ, kể cả những dòng hợp lệ đứng trước dòng lỗi. Ví dụ:

```text
[agent] ERROR: Python syntax error at line 3, column 12: invalid syntax
```

Nếu code lỗi khi chạy (exception, thiếu dependency, exit code khác 0 hoặc `state` không hợp lệ), agent in traceback/lỗi, dừng các lượt tiếp theo và thoát với mã 2. Nó lưu checkpoint của lượt thành công gần nhất; trạng thái job là `INTERRUPTED`. Ví dụ lỗi ở lượt 3 thì checkpoint là lượt 2, và `resume` thử lại từ lượt 3. Không tự retry một lượt đang lỗi. Code của job không đổi: sửa source cần `submit` job mới; nếu chỉ sửa môi trường/dependency thì có thể `resume` job cũ.


## SSH, xác thực và gửi code

Ví dụ chuyển file từ máy cá nhân và SSH vào VM (thay IP và tên user bằng thông tin đã được cấp):

```bash
scp sum_squares.py spulti@SPOT_VM_IP:~/sum_squares.py
ssh spulti@SPOT_VM_IP
```

Đăng nhập SSH xác định tài khoản Linux; agent còn cần xác thực chủ job với Spulti. Có hai cách:

- Nếu CLI `spulti` đã được cài trên VM: chạy `spulti login --api-url https://YOUR_CONTROL_PLANE_DOMAIN`. Agent đọc lại `~/.spulti/config.json` (hoặc đường dẫn `SPULTI_CONFIG`), không hỏi lại API key cho mỗi job.
- Nếu chỉ có agent: đặt URL như bên dưới; khi gửi job agent sẽ hỏi API key/JWT bằng nhập ẩn. Có thể dùng `SPULTI_API_KEY` trong môi trường cho chạy không tương tác. API key của user khác với token riêng từng job; agent tự tạo và đăng ký token job.

```bash
export SPULTI_API_URL=https://YOUR_CONTROL_PLANE_DOMAIN
spulti-agent submit ~/sum_squares.py --total-steps 100
```

Khi gọi trực tiếp bằng Python trên VM AWS/GCP, đặt thêm `--cloud-provider aws` hoặc `gcp`; launcher đã đặt mặc định theo provider. Trên local, mặc định `none` để không truy cập metadata.

Ví dụ output:

```text
[agent] REGISTERING JOB_ID=<UUID>; retry with resume <UUID>
[agent] JOB_ID=<UUID>
[agent] Resume: spulti-agent resume <UUID>
[agent] job=<UUID> RESUME_FROM=0 NEXT_STEP=1 TOTAL=100 STATE={}
step=1 sum=1
[agent] STEP=1 STATE={"running_sum": 1}
step=2 sum=5
[agent] STEP=2 STATE={"running_sum": 5}
```

ID được agent sinh ngẫu nhiên và đăng ký vào Control Plane, gắn với tài khoản Spulti đã xác thực. Code, hash SHA256 và cấu hình chạy được lưu trong `agent_programs`; thông tin tiến độ nằm trong `agent_jobs`. Một lần `submit` chủ động mới sẽ tạo một job mới. Nếu mất phản hồi lúc đăng ký, dùng `resume` với ID đã in để gửi lại cùng yêu cầu, không tạo thêm job.

Mặc định: delay 5 giây, heartbeat mỗi lượt hoàn thành và **checkpoint mỗi 10 giây**. Agent còn lưu khi Ctrl+C/SIGTERM/SIGHUP, cảnh báo thu hồi, lỗi chạy hoặc hoàn thành. `--checkpoint-every N` với N > 0 bật thêm lưu sau mỗi N bước; giá trị 0 chỉ tắt điều kiện theo số bước, không tắt bộ đếm thời gian. Agent chỉ giữ trạng thái thành công gần nhất, không tích lũy kết quả mọi lượt. Các file tạm dùng để trao đổi dữ liệu với Python process được xóa sau mỗi lượt; đó không phải checkpoint để recovery.

Bộ đếm thời gian vẫn được kiểm tra khi chờ delay và khi Python process đang chạy một lượt dài. Mỗi snapshot chứa số lượt **đã hoàn thành** cùng `state` tương ứng; thay đổi của lượt còn chạy chưa được nhận vào checkpoint. Bản local được ghi trước, rồi đồng bộ qua Control Plane tới S3. Khi VM mất đột ngột, VM mới lấy bản gần nhất đã upload thành công; không cần đã nhận cảnh báo thu hồi.

Ví dụ đang chạy lượt 38, lần đồng bộ gần nhất lưu lượt 37 thì resume chạy lại lượt 38 với `state` của lượt 37. Nếu lần đồng bộ gần nhất mới tới lượt 35 thì phải chạy lại từ lượt 36. Chu kỳ 10 giây không đảm bảo chỉ mất tối đa 10 giây tính toán: một lượt có thể chạy lâu, mạng/API có thể chậm hoặc upload thất bại.

Có thể đổi chu kỳ bằng `--checkpoint-seconds SECONDS` (0.1–3600), áp dụng cho lần chạy hiện tại. Giá trị mặc định là 10 cho cả `submit` và `resume`; tùy chọn này không thay đổi cấu hình job trong DB. Ví dụ:

```bash
spulti-agent submit sum_squares.py --total-steps 100 --checkpoint-seconds 10
spulti-agent resume JOB_ID --checkpoint-seconds 10
```

Khi có cảnh báo giữa một lượt, agent dừng process đang chạy rồi lưu trạng thái của lượt thành công gần nhất. Ví dụ đang chạy lượt 38 thì checkpoint là 37 và resume chạy lại lượt 38 từ đầu. Không khôi phục giữa một lệnh Python/vòng lặp nội bộ. Nếu lượt dở dang đã ghi file hoặc gọi dịch vụ bên ngoài, các tác dụng đó không được tự hoàn tác; code cần xử lý việc chạy lại nếu chúng có thể trùng lặp.

## Khôi phục theo job_id

Sau Ctrl+C hoặc khi đã có VM mới sẵn sàng, đăng nhập Spulti trên VM đó rồi chạy:

```bash
spulti-agent resume JOB_ID_DA_DUOC_CAP
```

Không cần truyền lại file code, tổng số bước, token job hay nội dung checkpoint. Agent sẽ:

1. Xác thực chủ job và tải code/cấu hình đã đăng ký.
2. Cấp token job mới qua API chủ job, vô hiệu hóa token cũ cho các request tiếp theo.
3. Tự gọi `GET /api/agent/jobs/{job_id}/checkpoint` để lấy checkpoint mới nhất.
4. Nạp bước và toàn bộ `workload_state` vào `spulti.state`, rồi chạy lượt kế tiếp.

Checkpoint của script dùng `version: 3`, `workload: "python_script"` và SHA256 của source để tránh khôi phục trạng thái cho sai code. Ví dụ sau lượt 4 của bài tổng bình phương: `last_completed_step: 4`, `workload_state: {"running_sum": 30}`. Lượt tiếp theo nhận `step_number == 5` và tổng 30, rồi cập nhật thành 55.

Demo tích hợp khi gọi `agent.py --job-id ...` vẫn dùng checkpoint version 2. File cũ chỉ định nghĩa `def step(...)` cần chuyển sang code thực thi ở cấp module như ví dụ trên và submit job mới. Agent không tự gọi hàm `step` hay chuyển đổi checkpoint version 2 thành version 3.

Code của một job là cố định. Muốn thay code hoặc số lượt thì `submit` job mới. Với thư mục state mới hoàn toàn, bước và trạng thái được khôi phục từ API; nếu cùng VM có checkpoint local mới hơn đã được xác thực, agent có thể đồng bộ bản đó trước khi chạy. Nếu chưa có checkpoint thì bắt đầu lại từ bước 1. Job đã hoàn thành không thực thi code lại.

Thông tin local nằm trong `~/.spulti-agent/`:

```text
jobs/<job_id>.json           # code/cấu hình và token job, quyền 0600 trên Linux
jobs/<job_id>.lock           # khóa chống hai lệnh chạy job trong cùng state-dir
checkpoints/<job_id>.json    # checkpoint gần nhất trên VM
heartbeats/<job_id>.json
interruptions/<job_id>.json
```

Có thể đổi thư mục bằng `--state-dir PATH` cho cả `submit` và `resume`. API key của user không được sao chép vào file job. Khóa local tự được OS nhả khi tiến trình chết; không cần xóa file `.lock` để chạy lại.

## API bổ sung

Các endpoint sau dùng `Authorization: Bearer <API key hoặc JWT của user>`:

| Endpoint | Chức năng |
| --- | --- |
| `PUT /api/agent/submissions/{job_id}` | Đăng ký code/cấu hình và hash token job; cùng ID/token/nội dung có thể retry |
| `GET /api/agent/jobs/{job_id}/submission` | Chủ job tải lại code/cấu hình |
| `POST /api/agent/jobs/{job_id}/agent-token` | Chủ job đổi token để chạy trên VM mới |

Các API checkpoint/heartbeat/interruption vẫn dùng `X-Agent-Token`. Người dùng khác không được đọc code hoặc đổi token của job.

## Phạm vi và kiểm thử

User vẫn phải có VM và quyền SSH hợp lệ. Không tạo VM thay thế, không tự lập lịch recovery, chưa gắn job với một lượt thuê máy và chưa có khóa chạy phân tán giữa nhiều VM. Dừng agent cũ trước khi chủ động `resume` trên máy khác; đổi token không phải lệnh kill tiến trình cũ. Các file dữ liệu/dependencies ngoài file Python chưa được tự chuyển sang VM mới.

Job chạy trong phiên lệnh hiện tại. Trên Linux, agent xử lý SIGHUP khi phiên SSH bị đóng nếu OS gửi tín hiệu đó; tiến trình bị SIGKILL hoặc VM mất đột ngột có thể không kịp lưu checkpoint cuối. Khi đó chỉ khôi phục được checkpoint đã lưu trước đó.

```bash
# node-agent/
python -m unittest discover -s tests -v
# control-plane/
python -m unittest discover -s tests -p test_submissions.py -v
```

Integration test chạy agent/FastAPI/SQLite thật, giả lập kho checkpoint trong bộ nhớ: in hello đủ 100 lần, từ chối lỗi cú pháp trước đăng ký, dừng sau runtime error, khôi phục trạng thái JSON/code trên state-dir mới, crash đột ngột không có cảnh báo rồi phục hồi từ checkpoint theo thời gian, không chạy lại job đã xong, ownership và thu hồi token cũ. Test node-agent còn kiểm tra lưu định kỳ trong lượt dài, trong delay, retry upload tạm lỗi và dừng process khi có cảnh báo. Các test không tạo VM hoặc gọi S3 thật.
