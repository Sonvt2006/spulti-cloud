# Chạy Catalog & Market Data Collector trên EC2

## 1. Cập nhật code và bật service

```bash
cd ~/spulti-cloud/control-plane

git branch --show-current
git status --short
# Chỉ pull khi đang ở đúng branch triển khai và không có sửa đổi cần giữ:
git pull --ff-only

# Kiểm tra Compose có dịch vụ beat:
docker compose config --quiet
docker compose config --services | grep -x beat

# Khởi chạy API, Worker và Celery Beat (Redis được chạy theo dependency nếu cần):
docker compose up -d --build api worker beat
docker compose ps
```

**Cần thấy `worker` và `beat` ở trạng thái Up.** Nếu `grep -x beat` không có kết quả, hoặc `no such service: beat`, phiên bản code trên EC2 chưa chứa bản scheduler mới. Hãy cập nhật đúng branch đã commit/push các thay đổi `compose.yaml`, `app/celery_app.py`, `app/tasks.py`, `app/services/market_data_collector.py`, rồi chạy lại. **Không tự thêm Beat vào một bản code chưa có task và lịch chạy.**

## 2. Kiểm tra giới hạn và cloud credentials

```bash
# Chuỗi rỗng ('') nghĩa là Collector không giới hạn số loại máy.
docker compose exec worker python -c "import os; print('MARKET_LIMIT_TYPES:', repr(os.getenv('MARKET_LIMIT_TYPES')))"

# Kiểm tra scheduler đã nạp các lịch:
docker compose exec worker python -c "from app.celery_app import celery_app; print(list(celery_app.conf.beat_schedule))"

# Kiểm tra danh tính AWS trong Worker (dùng IAM role/credentials EC2 đã thiết lập):
docker compose exec worker python -c "import boto3; print(boto3.client('sts', region_name='us-east-1').get_caller_identity()['Arn'])"

# Kiểm tra file GCP credentials được mount vào Worker:
docker compose exec worker python -c "from pathlib import Path; print(Path('/run/secrets/gcp-credentials.json').is_file())"
```

Nếu `MARKET_LIMIT_TYPES` trả về `'20'`, Market Collector vẫn chỉ đọc tối đa 20 loại máy từ Catalog. Cấu hình **không giới hạn** là `MARKET_LIMIT_TYPES` rỗng (ví dụ `MARKET_LIMIT_TYPES: "${MARKET_LIMIT_TYPES-}"` trong `compose.yaml`, và `.env` không ghi đè bằng số). Sau khi thay đổi cấu hình đã có, chạy `docker compose up -d --force-recreate worker beat`. Không dùng giá trị `0`.

## 3. Chạy Catalog: đồng bộ toàn bộ loại máy vào RDS

Không truyền tham số `limit_types`, do đó đồng bộ tất cả loại máy mà API liệt kê **trong Zone chỉ định**.

**GCP (`asia-southeast1-b`):**

```bash
docker compose exec worker python -c "from app.services.catalog import sync_catalog_gcp; print(sync_catalog_gcp('spulti-cloud', 'asia-southeast1', 'asia-southeast1-b'))"
```

**AWS (`us-east-1a`):**

```bash
docker compose exec worker python -c "from app.services.catalog import sync_catalog_aws; print(sync_catalog_aws('us-east-1', 'us-east-1a'))"
```

Ví dụ kết quả GCP khi **đã thử ở local**: `{'machine_types': 506, 'created': 486, 'refreshed': 20}`. Đó không phải số lượng bảo đảm sẽ thấy trên EC2. `created` và `refreshed` trong hàm này phản ánh số bản ghi liên kết machine type–Zone được tạo mới/làm mới.

Catalog **không tự chạy theo lịch** trong cấu hình này: cần chạy bước 3 khi muốn cập nhật danh sách máy hoặc thêm Zone.

## 4. Chạy thử Market Data Collector thủ công

Các lệnh sau gọi API cloud thật và ghi vào **RDS**. Nếu lấy tất cả loại máy, một lượt có thể mất nhiều thời gian và sử dụng đáng kể API quota.

**Thu thập giá:**

```bash
# AWS prices
docker compose exec worker python -c "from app.tasks import collect_market_data; print(collect_market_data('AWS', 'prices'))"

# GCP prices
docker compose exec worker python -c "from app.tasks import collect_market_data; print(collect_market_data('GCP', 'prices'))"
```

**Thu thập rủi ro:**

```bash
# AWS risks
docker compose exec worker python -c "from app.tasks import collect_market_data; print(collect_market_data('AWS', 'risks'))"

# GCP risks
docker compose exec worker python -c "from app.tasks import collect_market_data; print(collect_market_data('GCP', 'risks'))"
```

**Thu thập giá On-demand:**

```bash
docker compose exec worker python -c "from app.tasks import collect_market_data; print(collect_market_data('AWS', 'on_demand'))"
docker compose exec worker python -c "from app.tasks import collect_market_data; print(collect_market_data('GCP', 'on_demand'))"
```

GCP On-demand dùng Cloud Billing Catalog API và credentials GCP sẵn có; bật
`cloudbilling.googleapis.com` trong project cấp credentials. Hỗ trợ E2 predefined,
gồm `e2-micro/small/medium`, standard, highcpu và highmem. Region được suy ra từ
`MARKET_GCP_ZONES`; đặt `asia-southeast1-b,asia-northeast1-a` nếu cần Singapore
và Tokyo, rồi recreate worker. `MARKET_LIMIT_TYPES` giới hạn các E2 được hỗ trợ
sau khi lọc các loại máy khác. Kết quả có `prices_received`, `prices_saved` và
`prices_missing`; `partial` báo thiếu giá ở một phần region. Collector lưu vào
`spot_prices` bằng `price_type=ON_DEMAND`, không cần migration.
Xem [cách tính giá và giới hạn lịch sử](DASHBOARD.md).

`status: ok` nghĩa là lượt thu thập hoàn thành; `status: skipped` thường do khóa ngăn tác vụ cùng loại chạy trùng; `status: empty` là không có việc/dữ liệu để xử lý. Nếu gặp `No GCP/AWS machine types found`, đồng bộ Catalog tương ứng ở bước 3.

**Tránh chạy thử thủ công đúng lúc Beat đang chạy cùng một loại task.** Lệnh thử trực tiếp gọi hàm trong container, không gửi vào queue Celery; Redis lock vẫn được áp dụng nếu phiên bản task có cơ chế này.

## 5. Kiểm tra dữ liệu trong RDS từ EC2

Không cần mở `psql`: dùng `DATABASE_URL` hiện có của Worker.

```bash
docker compose exec -T worker python - <<'PY'
from sqlalchemy import text
from app.database import SessionLocal

queries = {
    "Catalog": "SELECT provider, COUNT(*) FROM machine_types GROUP BY provider ORDER BY provider",
    "Prices": """SELECT mt.provider, COUNT(*), MAX(sp.recorded_at)
        FROM spot_prices sp JOIN machine_types mt ON mt.id = sp.machine_type_id
        GROUP BY mt.provider ORDER BY mt.provider""",
    "Risks": """SELECT mt.provider, COUNT(*), MAX(sr.observed_at)
        FROM spot_risks sr JOIN machine_types mt ON mt.id = sr.machine_type_id
        GROUP BY mt.provider ORDER BY mt.provider""",
}

with SessionLocal() as db:
    for title, query in queries.items():
        print(f"\n== {title} ==")
        for row in db.execute(text(query)):
            print(tuple(row))
PY
```

Dữ liệu được chống trùng theo quy tắc hiện tại, nên **Collector chạy thành công không đồng nghĩa số dòng tăng sau mỗi lần chạy**. Xem log và `MAX(recorded_at)` / `MAX(observed_at)` để đánh giá dữ liệu mới.

## 6. Kiểm tra tự động thu thập

```bash
docker compose ps
docker compose logs -f --tail=100 beat worker
```

Khi code hiện tại đặt Celery timezone là `UTC`, lịch theo **giờ Việt Nam (UTC+7)** là:

| Task | Lịch tự động |
|---|---|
| AWS Spot Prices | Phút **00, 15, 30, 45** mỗi giờ |
| GCP Spot Prices | Phút **05, 20, 35, 50** mỗi giờ |
| AWS Spot Risks | **07:20** mỗi ngày |
| GCP Spot Risks | **07:35** mỗi ngày |
| AWS On-demand Prices | **07:50** mỗi ngày |
| GCP On-demand Prices | **08:05** mỗi ngày |

Đây là **giờ gửi task**, không phải giờ hoàn thành. Beat gửi công việc vào Redis, Worker gọi AWS/GCP APIs và lưu dữ liệu vào RDS. **Không cần bật máy Spot** để thu thập thị trường.

**Khi nào hệ thống thu thập?** Khi **EC2 bật và các container `redis`, `beat`, `worker` đang hoạt động**. Không cần bật laptop hay VS Code. Nếu EC2/container dừng, hệ thống không thu thập; lịch bỏ lỡ **không tự chạy bù** với cấu hình này.

## 7. Lệnh vận hành nhanh và lỗi thường gặp

```bash
# Sau khi cập nhật code, rebuild/recreate các service cần thiết:
docker compose up -d --build api worker beat

# Xem log:
docker compose logs --tail=100 beat worker

# Dừng lịch nhưng vẫn giữ API/Worker:
docker compose stop beat

# Bật lại lịch:
docker compose up -d beat
```

| Lỗi | Cách kiểm tra |
|---|---|
| `no such service: beat` | Chưa cập nhật Compose/scheduler đúng branch. |
| `NoCredentialsError` (AWS) | Kiểm tra IAM role gắn trên EC2 và quyền AWS SDK. Không chép Access Key laptop lên EC2. |
| GCP thiếu credentials | Kiểm tra file mount từ `/etc/spulti/secrets/gcp-credentials.json` tới `/run/secrets/gcp-credentials.json`. |
| `No GCP/AWS machine types found` | Chạy bước 3; xác nhận dữ liệu nằm trong **RDS** mà Worker đang dùng. |
| Chỉ thu 20 loại máy | `MARKET_LIMIT_TYPES` trong Worker vẫn là `'20'`. |
| Beat gửi task nhưng chưa có hàng mới | Xem Worker logs, quyền cloud API, Redis lock và cơ chế loại bỏ bản ghi trùng. |

> **Lưu ý về phiên bản:** Bản `main` trên GitHub được đối chiếu lúc viết README chưa chứa `beat`/Market Scheduler. Đảm bảo các thay đổi mới đã được commit/push và cập nhật lên EC2 trước khi dùng các lệnh trên.
