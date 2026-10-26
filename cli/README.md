# Spulti CLI

Công cụ dòng lệnh `spulti` để làm việc với control plane. Hiện có đăng nhập và quản lý API key.

Chỉ dùng thư viện chuẩn của Python (3.11 trở lên), không cần cài thêm gói nào.

## Cài đặt

```bash
cd cli
pip install .
spulti --help
```

## Đăng nhập

```bash
spulti login --api-url http://<EC2_PUBLIC_IP>
```

CLI hỏi username (hoặc email) và mật khẩu, rồi:

1. Gọi `POST /api/auth/login` để lấy JWT ngắn hạn.
2. Dùng JWT gọi `POST /api/api-keys` để tạo API key dài hạn, tên mặc định là `<username>-<tên máy>`.
3. Chỉ lưu API key vào `~/.spulti/config.json` (quyền `600`). Mật khẩu và JWT không được lưu.

Đăng nhập lại trên cùng máy sẽ tự thu hồi key cũ. Lần sau không cần truyền `--api-url` vì địa chỉ đã được lưu.

Control plane hiện chưa có HTTPS nên mật khẩu đi qua mạng dưới dạng không mã hóa, CLI sẽ cảnh báo khi địa chỉ không phải `localhost`.

## Các lệnh

```bash
spulti whoami                                    # xem tài khoản đang đăng nhập
spulti api-key list                              # liệt kê key, đánh dấu key của máy này
spulti api-key create ci --expires-in-days 30    # tạo key cho script/CI, in key ra một lần
spulti api-key revoke <ID>                       # thu hồi key theo ID trong danh sách
spulti logout                                    # thu hồi key của máy này và xóa cấu hình
```

## Biến môi trường

| Biến | Ý nghĩa |
|------|---------|
| `SPULTI_API_URL` | Địa chỉ control plane, ưu tiên hơn giá trị trong file cấu hình |
| `SPULTI_API_KEY` | Dùng key này thay cho key trong file cấu hình, tiện cho script hoặc CI |
| `SPULTI_CONFIG` | Đường dẫn file cấu hình, mặc định `~/.spulti/config.json` |
