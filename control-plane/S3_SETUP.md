# Thiết lập S3 và phát hành Node Agent

Làm bước này khi triển khai checkpoint/resume trên cloud, sau khi có EC2 control
plane và trước khi chạy job. S3 là dịch vụ quản lý sẵn; không cài S3 lên EC2.

| Dữ liệu | Nơi lưu | Ai truy cập |
| --- | --- | --- |
| Checkpoint JSON | Bucket private, prefix `checkpoints/` | API control plane đọc/ghi bằng IAM role |
| Gói chương trình Node Agent `.tar.gz` | Bucket release riêng, prefix `agent/releases/` | Người triển khai upload; VM AWS/GCP tải qua HTTPS CloudFront |
| Job, tiến độ, tham chiếu tới object S3 | PostgreSQL/RDS | Control plane |

Agent gửi checkpoint qua API; VM chạy job không cần quyền S3 để lưu checkpoint.
Gói agent chứa chương trình, không chứa checkpoint hay trạng thái của người dùng.
Hướng dẫn dùng hai bucket để quyền phân phối chương trình không áp dụng cho dữ
liệu checkpoint. Các tên bucket, account ID và domain bên dưới đều phải thay.

## 1. Tạo hai bucket bằng AWS Console

Vào **S3 → General purpose buckets → Create bucket** và tạo lần lượt:

- `spulti-checkpoints-ACCOUNT_ID-ap-southeast-1`
- `spulti-releases-ACCOUNT_ID-ap-southeast-1`

Chọn region gần control plane, ví dụ Singapore `ap-southeast-1`. Tên bucket phải
khả dụng; dùng tên thực tế của bạn trong toàn bộ hướng dẫn. Với cả hai bucket:

1. Bucket type: **General purpose**.
2. Object Ownership: **Bucket owner enforced**, ACLs disabled.
3. Giữ **Block all public access** bật.
4. Default encryption: **SSE-S3**. Nếu chọn SSE-KMS, cần cấu hình thêm quyền KMS
   cho bên ghi/đọc; policy mẫu dưới đây chỉ dành cho SSE-S3.
5. Có thể để Versioning disabled ở bước đầu. Code tạo key checkpoint mới mỗi
   lần lưu; bật versioning không thay thế cơ chế checkpoint của ứng dụng.
6. Chọn **Create bucket**. Không cần bật static website hosting hay tạo thư mục
   trước; prefix được hình thành khi upload object.

Xem [hướng dẫn tạo bucket của AWS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/create-bucket-overview.html).

## 2. Cấp quyền checkpoint cho EC2 control plane

Trong **IAM → Roles**, tạo role với trusted entity **AWS service → EC2**, hoặc
bổ sung policy vào role control plane hiện có. Thêm inline policy sau, thay
`CHECKPOINT_BUCKET` bằng tên bucket checkpoint:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWriteCheckpoints",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::CHECKPOINT_BUCKET/checkpoints/*"
    }
  ]
}
```

Trong **EC2 → Instances**, chọn máy control plane → **Actions → Security →
Modify IAM role** và gắn role này. Nếu máy đã có role cấp phát VM, thêm policy
vào role đó để giữ các quyền đang dùng. Policy trên chỉ cấp quyền checkpoint,
không thay thế quyền EC2/GCP mà worker cần để tạo/xóa VM.

API dùng `boto3` lấy credentials của instance role; không ghi access key vào
archive hay `.env`. **API cần quyền S3**, kiểm tra STS trong worker thôi chưa đủ.
Với Docker bridge, nếu container không lấy được credentials từ IMDS, kiểm tra
EC2 metadata options: bật endpoint, yêu cầu **IMDSv2**, đặt response hop limit
**2** cho môi trường container. Xem [AWS metadata options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html).

EC2 cần kết nối HTTPS tới S3 (qua internet/NAT hoặc S3 VPC endpoint phù hợp).
Bucket policy, endpoint policy hay tổ chức có explicit deny vẫn có thể chặn IAM
allow ở trên. Role của API không cần `ListBucket` hoặc `DeleteObject` cho luồng
checkpoint hiện tại.

## 3. Đóng gói và upload Node Agent

Chạy trên Ubuntu/WSL, tại **thư mục gốc `spulti-cloud`**, nơi có `node-agent/`:

```bash
tar -czf /tmp/node-agent-recovery.tar.gz \
  --exclude=tests --exclude=__pycache__ --exclude='*.pyc' \
  --exclude=.venv --exclude='.env*' --exclude='.state*' \
  -C node-agent .
tar -tzf /tmp/node-agent-recovery.tar.gz
sha256sum /tmp/node-agent-recovery.tar.gz
```

File đầu ra nằm ở `/tmp/node-agent-recovery.tar.gz`, không nằm trong repo.
Archive cần có `./agent.py`, `./managed.py` và các module đồng cấp ngay ở gốc,
không bọc thêm thư mục `node-agent/`. Kiểm tra danh sách để loại mọi credentials,
state hoặc dữ liệu riêng đã đặt trong thư mục source trước khi phát hành.
Nếu đã có archive hợp lệ thì dùng luôn file đó; nén lại có thể đổi SHA256.

Máy upload cần AWS CLI v2 và phiên đăng nhập AWS hợp lệ (xem
[README.local.md](README.local.md) để cài CLI/đăng nhập). Tài khoản phát hành cần
quyền `s3:PutObject` trên `arn:aws:s3:::RELEASE_BUCKET/agent/releases/*`; quyền
này thuộc người phát hành, không phải role đọc/ghi checkpoint.

```bash
SPULTI_RELEASE_BUCKET='THAY_TEN_BUCKET_RELEASE'
SPULTI_RELEASE_SHA256=$(sha256sum /tmp/node-agent-recovery.tar.gz | cut -d ' ' -f 1)
aws s3 cp /tmp/node-agent-recovery.tar.gz \
  "s3://${SPULTI_RELEASE_BUCKET}/agent/releases/${SPULTI_RELEASE_SHA256}/node-agent.tar.gz" \
  --region ap-southeast-1
```

Hoặc dùng **S3 → bucket release → Upload** và upload vào prefix
`agent/releases/<SHA256>/`. Không ghi đè một key đã phát hành; mỗi phiên bản dùng
key mới và giữ các phiên bản cũ còn được attempt/recovery tham chiếu.

## 4. Cấp URL HTTPS bền vững qua CloudFront

Installer của managed job tải bằng HTTP thông thường, không ký request S3.
Vì vậy `s3://...` hoặc URL HTTPS trực tiếp tới object private không dùng được.
Presigned URL có hạn sử dụng cũng không phù hợp để cấu hình lâu dài: máy thay
thế có thể được tạo sau khi URL hết hạn.

Dùng **CloudFront với Origin Access Control (OAC)** để giữ bucket private và
phân phối riêng gói chương trình:

1. Vào **CloudFront → Create distribution**, chọn origin là bucket **release**
   qua S3 REST endpoint (không chọn website endpoint). Để origin path trống.
2. Chọn **Origin access control**, tạo OAC và chọn **Sign requests (recommended)**.
3. Viewer protocol policy: **Redirect HTTP to HTTPS** hoặc **HTTPS only**.
   Allowed methods: `GET, HEAD`. Không yêu cầu signed viewer URLs/cookies vì
   installer hiện chưa cấp credential CloudFront cho VM.
4. Tạo distribution. Áp dụng bucket policy mà Console đề xuất; chỉ cho phép
   distribution này đọc prefix release. Mẫu dưới đây thay đủ ba placeholder:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "CloudFrontReadAgentReleases",
         "Effect": "Allow",
         "Principal": {"Service": "cloudfront.amazonaws.com"},
         "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::RELEASE_BUCKET/agent/releases/*",
         "Condition": {
           "StringEquals": {
             "AWS:SourceArn": "arn:aws:cloudfront::ACCOUNT_ID:distribution/DISTRIBUTION_ID"
           }
         }
       }
     ]
   }
   ```

5. Chờ distribution deploy xong. URL sẽ có dạng:
   `https://DISTRIBUTION_DOMAIN/agent/releases/<SHA256>/node-agent.tar.gz`.

Bucket vẫn bật Block Public Access, nhưng **gói agent qua CloudFront được phép
đọc công khai**. Chỉ đưa source/release có thể phân phối vào bucket này; không
đưa checkpoint hoặc secrets vào đó. Nếu tổ chức yêu cầu release private cả ở
phía người tải, cần thiết kế thêm cơ chế xác thực/phân phối phù hợp; installer
hiện tại chưa thực hiện việc này.

Cấu hình OAC theo [tài liệu AWS](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html).

Kiểm tra từ máy không cần credentials AWS, và từ mạng VM sẽ chạy job:

```bash
curl --fail --location \
  'https://DISTRIBUTION_DOMAIN/agent/releases/SHA256/node-agent.tar.gz' \
  --output /tmp/node-agent-downloaded.tar.gz
sha256sum /tmp/node-agent-downloaded.tar.gz
```

Hash tải về phải trùng file đã upload. VM cũng cần outbound HTTPS/DNS để tải
release, cài gói hệ điều hành và gọi control plane.

## 5. Cấu hình control plane và kiểm tra S3

Trong `control-plane/.env` trên EC2:

```dotenv
SPULTI_CHECKPOINT_BUCKET=THAY_TEN_BUCKET_CHECKPOINT
AWS_REGION=ap-southeast-1
SPULTI_PUBLIC_API_URL=https://api.example.com
SPULTI_AGENT_RELEASE_URL=https://DISTRIBUTION_DOMAIN/agent/releases/SHA256/node-agent.tar.gz
SPULTI_AGENT_RELEASE_SHA256=THAY_SHA256_64_KY_TU
```

`SPULTI_PUBLIC_API_URL` cần domain/TLS hợp lệ, truy cập được từ AWS/GCP.
Compose hiện chỉ cung cấp HTTP cổng 80; phải thiết lập TLS ở reverse proxy/load
balancer phía trước trước khi dùng managed jobs. Không dùng `localhost` làm URL
cho VM trên cloud.

Từ thư mục `control-plane`, sau khi đã chuẩn bị RDS và các mount của Compose:

```bash
docker compose build
docker compose run --rm --no-deps api python -m app.init_db
docker compose up -d --build
```

Sau khi sửa `.env`, dùng `up -d` để recreate container nhận cấu hình; chỉ
`restart` không cập nhật environment. Kiểm tra identity của **API**:

```bash
docker compose exec api python -c 'import boto3; print(boto3.client("sts").get_caller_identity()["Arn"])'
```

Kiểm tra đúng đường ghi/đọc checkpoint mà ứng dụng dùng:

```bash
docker compose exec -T api python - <<'PY'
from uuid import uuid4
from app.checkpoint_storage import S3CheckpointStore
store = S3CheckpointStore()
payload = {"job_id": f"deploy-smoke-{uuid4().hex}", "last_completed_step": 0, "status": "RUNNING"}
reference = store.save(payload)
assert store.load(reference) == payload
print("S3 checkpoint read/write OK:", reference["key"])
PY
```

Lệnh tạo một object kiểm tra nhỏ, không tạo VM. Có thể xóa object in ra bằng
S3 Console với tài khoản quản trị; runtime role không cần quyền xóa.
Healthcheck `/health` thành công **không chứng minh S3 hoạt động**.
Tiếp tục [Instance Manager](INSTANCE_MANAGER.md) để cấu hình image/network,
credentials cấp VM và catalog/giá, rồi [Recovery](RECOVERY.md) để gắn job vào máy.

### Nếu control plane chạy trên laptop

Bucket/release vẫn dùng như trên. `compose.local.yaml` truyền AWS credentials
cho cả API và worker từ terminal hiện tại. Export credentials tạm thời theo
[hướng dẫn local](README.local.md), cấp quyền checkpoint cho identity đó và
điền bucket/region vào `.env.local`, rồi chạy từ thư mục `control-plane`:

```bash
docker compose --env-file .env.local -f compose.local.yaml up -d --build
```

Dùng cùng bộ cờ này thay cho `docker compose` trong các lệnh kiểm tra ở trên.
Không ghép `compose.yaml` của EC2 vào stack local. Khi credentials hết hạn,
đăng nhập/export lại rồi recreate API/worker. Dashboard không cần S3; chỉ cấu
hình bucket khi bật checkpoint remote.

## 6. Lỗi thường gặp và lưu ý vận hành

| Hiện tượng | Kiểm tra |
| --- | --- |
| `Chưa cấu hình SPULTI_CHECKPOINT_BUCKET` | Điền bucket và recreate API |
| `Unable to locate credentials` | Role của EC2, IMDS từ API container; local cần export credentials và recreate API |
| `AccessDenied` khi checkpoint | Policy `PutObject`/`GetObject` đúng prefix; bucket/endpoint policy; quyền KMS nếu dùng SSE-KMS |
| Release trả `403` | Đúng object key, OAC, bucket policy/distribution ARN và distribution đã deploy |
| SHA256 không khớp | Hash của chính archive đã upload, không dùng hash của lần nén trước |
| API chạy nhưng job không khôi phục | Kiểm tra S3 smoke test, HTTPS API/release, worker/beat và sự kiện máy |

Không tự đặt lifecycle xóa toàn bộ `checkpoints/` sau vài ngày: RDS có thể còn
tham chiếu checkpoint đó để resume. Code hiện tạo object mới ở mỗi lần lưu và
chưa có quy trình dọn object cũ; theo dõi dung lượng/request và chỉ dọn khi biết
chắc job/attempt không còn cần dữ liệu.

Script thử nghiệm `app/scripts/bootstrap_node_agent.sh` là luồng khác: đang ghi
cố định bucket, region, key và checksum, tải trực tiếp qua boto3 nên VM đó cần
quyền `s3:GetObject` cho release. Thay `.env` không sửa các hằng số của script.
Managed jobs dùng `SPULTI_AGENT_RELEASE_URL`/`SPULTI_AGENT_RELEASE_SHA256` và
installer được sinh bởi `app/services/managed_agent.py` cho cả AWS/GCP.
