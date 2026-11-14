# PyTorch: một lần chạy file = một epoch

Agent vẫn chạy toàn bộ file Python `total_steps` lần, mỗi lần trong một tiến
trình mới. `TorchCheckpoint` nạp model và optimizer của epoch trước; code của
bạn chạy một epoch rồi gọi `save()`. Không đặt vòng lặp nhiều epoch trong file.

## Chạy thử local (CPU, không cần Control Plane)

Từ thư mục `node-agent`, dùng Python 3.10+ có PyTorch:

```powershell
..\.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu
..\.venv\Scripts\python.exe agent.py run examples/torch_regression.py --job-id torch-demo --total-steps 20
```

Ví dụ tự tạo dữ liệu `y = 3x + 2 + noise`, không cần tải dataset. Mỗi epoch đi
qua 256 mẫu với batch size 32, dùng Linear + Adam và in loss cuối epoch.

Nhấn Ctrl+C rồi chạy lại **cùng lệnh, job ID, code, tổng epoch và state-dir** để
tiếp tục. Epoch đang dở sẽ chạy lại. Job đã hoàn thành không chạy lại; muốn thử
lại từ đầu, dùng job ID mới. Local chỉ giữ checkpoint trên đĩa máy này.

## Tích hợp vào file training

```python
import torch
from spulti_torch import TorchCheckpoint

model = ...
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
# Nếu có scheduler, khởi tạo nó trước helper và truyền scheduler=scheduler.
checkpoint = TorchCheckpoint(model, optimizer)

model.train()
for inputs, targets in train_loader:  # Một epoch duy nhất
    optimizer.zero_grad()
    loss = loss_fn(model(inputs), targets)
    loss.backward()
    optimizer.step()

# Nếu có scheduler: scheduler.step() trước khi save.
checkpoint.save(metrics={"loss": float(loss.item())})
print(f"Finished epoch {checkpoint.epoch}")
```

Không cần trực tiếp import `spulti.state` hoặc `step_number`: helper sử dụng
chúng bên trong. `checkpoint.epoch` bắt đầu từ 1. Tạo dataset và model trước
helper; bắt đầu iteration của DataLoader sau helper để RNG đã được khôi phục.

## Nội dung và tính nhất quán của checkpoint

- File `.pt`: model `state_dict`, optimizer `state_dict`, scheduler nếu có,
  epoch, RNG của Python, PyTorch CPU và CUDA nếu có.
- JSON state: tham chiếu `_spulti_torch` gồm epoch, tên file, SHA-256 và metrics.
  Không đưa tensor vào JSON state (giới hạn toàn bộ state là 8 KiB).
- Ghi file tạm, `fsync`, rồi đổi tên nguyên tử. Giữ file epoch trước trong suốt
  epoch đang chạy. Chỉ khi tiến trình con kết thúc thành công, agent mới nhận
  tham chiếu mới và lưu checkpoint **ngay sau mỗi epoch**, bất kể chu kỳ thường.
- Với remote, agent upload file snapshot trước, rồi commit JSON/tiến độ qua API.
  Epoch kế tiếp chỉ bắt đầu sau khi checkpoint này được xác nhận thành công.
- Nếu code lỗi sau `save()`, tham chiếu mới không được commit. File thừa có thể
  nằm trong snapshot nhưng khi phục hồi helper chỉ đọc file mà JSON chỉ định,
  không tự chọn file có epoch lớn nhất.
- Nếu quên `save()`, epoch bị báo lỗi, không tăng tiến độ. Nếu file thiếu, sai
  checksum hoặc sai epoch, helper dừng thay vì âm thầm training từ đầu.
- Sau khi nạp epoch trước, helper dọn các file epoch cũ hơn. Thông thường giữ
  tối đa hai bản `.pt`. Snapshot hiện có giới hạn 256 MiB cho toàn bộ archive,
  vì vậy cơ chế này phục vụ model nhỏ.

Local mặc định lưu tại:

```text
state/checkpoints/torch-demo.json
state/files/torch-demo/torch/epoch-00000019.pt
state/files/torch-demo/torch/epoch-00000020.pt
```

## Chạy trên VM và phục hồi qua S3

1. Đóng gói và phát hành agent mới bằng `package_agent.py`; gói phải có
   `spulti_torch.py` và `local_run.py`. Cập nhật URL/SHA-256 release của control
   plane theo quy trình triển khai hiện có.
2. Cài PyTorch vào đúng Python interpreter mà job service sử dụng
   (`/usr/bin/python3`) qua image hoặc startup script **trước khi job bắt đầu**.
   Cấu hình này cần được giữ cho VM thay thế. Control plane không cần PyTorch.
3. Upload `examples/torch_regression.py` trên dashboard, đặt `total_steps` bằng
   số epoch, `step_delay=0`. Managed job Python mới tự bật file checkpoint.
4. Hoặc submit qua SSH, với thư mục checkpoint có quyền ghi:

   ```bash
   spulti-agent submit torch_regression.py --total-steps 20 --step-delay 0 --checkpoint-dir "$HOME/torch-checkpoints"
   spulti-agent resume JOB_ID --checkpoint-dir "$HOME/torch-checkpoints"
   ```

Managed recovery tải code và snapshot S3 lên VM mới, khôi phục JSON state và
chạy lại file cho epoch tiếp theo. Submit SSH độc lập cần gọi `resume` thủ công.
Job managed hoàn thành vẫn tự xóa VM; bản cuối nằm trên S3 qua checkpoint API.

## Phạm vi hỗ trợ

Ví dụ kiểm thử trên CPU, một process, một model/optimizer và một helper mỗi
epoch. Gọi `save()` một lần ở cuối epoch, sau mọi cập nhật model/scheduler.
Dataset, cấu trúc model, các hyperparameter ngoài optimizer và môi trường chạy
phải được tái tạo nhất quán. RNG riêng của NumPy, `torch.Generator` tự tạo,
DataLoader worker, AMP GradScaler và distributed training chưa được helper lưu.
Không đảm bảo kết quả bit-for-bit khi thay đổi thiết bị, phiên bản thư viện hoặc
dùng phép toán không xác định. Trạng thái không phải checkpoint giữa minibatch.

PyTorch được import chỉ khi workload dùng helper, nên các job Python cũ không
cần cài PyTorch. File được đọc bằng `torch.load(..., weights_only=True)`.
Tham khảo [hướng dẫn checkpoint chính thức của PyTorch](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html).

## Kiểm thử

```powershell
..\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Test PyTorch so sánh model, Adam, scheduler và RNG giữa chạy liền mạch và chạy
bị lỗi sau khi ghi epoch 2 rồi phục hồi snapshot sang thư mục VM mới; kiểm tra
thêm agent chết đột ngột và commit checkpoint remote thất bại. Khi môi
trường chưa có PyTorch, các test training được đánh dấu skip.
