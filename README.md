# Spulti-cloud

## Chạy trên máy cá nhân

Xem [hướng dẫn chạy local trên Windows 11 và Ubuntu](control-plane/README.local.md): cài AWS/GCP CLI và Python SDK, cấu hình credentials, chạy API/worker cùng PostgreSQL local và thử các cloud adapter.

Tài liệu khác: [triển khai trên EC2](control-plane/README.md), [Spulti CLI](cli/README.md), [quy trình đóng góp](CONTRIBUTING.md).

## About

Spulti-cloud là nền tảng thuê máy spot đa đám mây cho AWS và Google Cloud.
Máy spot có giá rẻ hơn máy on-demand, nhưng nhà cung cấp có thể thu hồi chúng bất cứ lúc nào mà gần như không báo trước. Giá cả và tần suất thu hồi máy cũng khác nhau tùy theo nhà cung cấp, khu vực và thời điểm. Spulti-cloud sẽ thay bạn so sánh giá, giám sát và khôi phục, giúp bạn huấn luyện các mô hình học máy trên tài nguyên spot giá rẻ mà không bị mất tiến trình.
