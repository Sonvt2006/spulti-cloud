# Thiết lập S3 và phát hành Node Agent

Hướng dẫn này dành cho **control plane chạy trên EC2**, thao tác chủ yếu bằng
AWS Console. Dùng **một bucket duy nhất** cho cả checkpoint và gói agent.
Nếu đã có bucket, dùng lại bucket đó và bắt đầu ở **bước 2**.

Bạn sẽ làm theo thứ tự: **tạo bucket → cấp quyền cho EC2 → upload agent → tạo
link tải agent → điền `.env` và kiểm tra**.

Trong cùng bucket, chia dữ liệu theo hai đường dẫn:

```text
spulti-sonvt/
├── checkpoints/       # Trạng thái công việc; chỉ control plane đọc/ghi
└── agent/releases/    # Gói cài agent; CloudFront được phép đọc
```

**Một bucket, một IAM role cho EC2.** Dùng cùng tên bucket trong mọi policy dưới
đây, nhưng giữ nguyên phần đường dẫn để giới hạn quyền:

| Phần cần làm | Làm ở đâu? | Đường dẫn được cấp quyền |
| --- | --- | --- |
| Cho EC2 đọc/ghi checkpoint | IAM → role của EC2 → Permissions | `checkpoints/*` |
| Bạn upload file agent | S3 → bucket → Upload | `agent/releases/*` |
| Cho CloudFront đọc file agent | S3 → bucket → Permissions → Bucket policy | `agent/releases/*` |

Checkpoint là trạng thái công việc để chạy tiếp khi VM bị mất. Release là file
`.tar.gz` chứa chương trình Node Agent để cài lên VM mới. Agent gửi checkpoint
qua API; API mới là bên ghi/đọc S3.

Ghi lại thông tin sau để dùng xuyên suốt. Các tên dưới đây chỉ là **ví dụ**,
thay bằng tên bạn đã tạo; không tạo lại bucket nếu đã có:

| Thông tin | Ví dụ |
| --- | --- |
| Bucket dùng chung | `spulti-sonvt` |
| Region của bucket | Singapore: `ap-southeast-1` |
| EC2 control plane | Máy đang hoặc sẽ chạy Docker Compose cho API |

## 1. Tạo một bucket bằng AWS Console

Vào **S3 → General purpose buckets → Create bucket**. Đặt tên, ví dụ
`spulti-sonvt`; nếu tên đã được dùng, chọn tên khác khả dụng.

Nếu trước đó đã tạo hai bucket, chọn một bucket để tiếp tục hướng dẫn này.
Không cần tạo thêm hay xóa bucket còn lại. Nếu đã upload agent ở bucket khác,
upload lại vào bucket đã chọn và dùng link CloudFront trỏ tới bucket đó.

Chọn region gần control plane, ví dụ Singapore `ap-southeast-1`. Dùng cùng tên
bucket thực tế xuyên suốt hướng dẫn, với các thiết lập:

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

## 2. Cho EC2 quyền đọc/ghi đường dẫn checkpoints/

**Bước này cấp quyền cho `checkpoints/*` trong bucket dùng chung.**
Role là danh tính AWS gắn cho EC2. Policy là đoạn JSON quy định role được làm gì.
Bạn sẽ thêm policy vào role, sau đó bảo đảm role đó đã gắn vào máy control plane.

### 2.1. Kiểm tra EC2 đã có role chưa

1. Mở **EC2 → Instances**.
2. Chọn **máy control plane** (máy chạy API, không phải máy spot chạy job).
3. Mở tab **Security**, xem mục **IAM Role**.
4. Nếu có tên role, bấm tên đó để mở trang IAM của role, rồi sang **2.3**.
5. Nếu chưa có role, làm **2.2**.

### 2.2. Tạo role nếu EC2 chưa có

1. Mở **IAM → Roles → Create role**.
2. Trusted entity type: chọn **AWS service**.
3. Service/use case: chọn **EC2**, bấm **Next**.
4. Ở bước Add permissions, chưa cần chọn policy, bấm **Next**.
5. Role name: điền `spulti-control-plane-role`.
6. Bấm **Create role**, rồi mở role vừa tạo.

### 2.3. Thêm policy checkpoint vào role

1. Trong trang role, mở tab **Permissions**.
2. Bấm **Add permissions → Create inline policy**.
3. Chọn trình soạn thảo **JSON**, thay nội dung bằng đoạn sau:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWriteCheckpoints",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject"],
      "Resource": "arn:aws:s3:::TEN_BUCKET/checkpoints/*"
    }
  ]
}
```

4. Thay **chỉ** `TEN_BUCKET` bằng tên bucket dùng chung của bạn.
   Ví dụ bucket `spulti-sonvt` thì dòng Resource phải là:

   ```text
   "Resource": "arn:aws:s3:::spulti-sonvt/checkpoints/*"
   ```

5. Bấm **Next**, đặt Policy name là `SpultiCheckpointReadWrite`.
6. Bấm **Create policy**.

Đoạn JSON này được dán trong **IAM role**, không dán vào Bucket policy của S3.
Giữ nguyên `/checkpoints/*`; không đổi Resource thành toàn bộ `TEN_BUCKET/*`.
Xem [thao tác thêm inline policy của AWS](https://docs.aws.amazon.com/IAM/latest/UserGuide/access_policies_manage-attach-detach.html).

### 2.4. Gắn role vào EC2 nếu vừa tạo mới

Nếu bạn thêm policy vào role vốn đã gắn với EC2, bỏ qua mục này.

1. Quay lại **EC2 → Instances**, chọn máy control plane.
2. Chọn **Actions → Security → Modify IAM role**.
3. Chọn `spulti-control-plane-role`, bấm **Update IAM role**.

Xem [cách gắn role của AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/attach-iam-role.html).

**Xong bước 2 khi:** EC2 hiển thị đúng IAM Role, và trong Permissions của role có
`SpultiCheckpointReadWrite`. Đây mới là quyền lưu checkpoint; quyền tạo/xóa máy
của worker được cấu hình riêng theo [Instance Manager](INSTANCE_MANAGER.md).
Không thay role sẵn có bằng role mới nếu role cũ còn cấp các quyền ứng dụng cần.

## 3. Đóng gói và upload vào agent/releases/

**Dùng lại bucket ở bước 1, upload gói agent vào `agent/releases/`.**
Bạn thao tác trên máy có mã nguồn repo, ví dụ laptop Ubuntu/WSL.

### 3.1. Tạo file nếu chưa có

Mở terminal tại thư mục gốc **`spulti-cloud`** (thư mục chứa `node-agent/`):

```bash
tar -czf /tmp/node-agent-recovery.tar.gz \
  --exclude=tests --exclude=__pycache__ --exclude='*.pyc' \
  --exclude=.venv --exclude='.env*' --exclude='.state*' \
  -C node-agent .
sha256sum /tmp/node-agent-recovery.tar.gz
```

Nếu đã tạo file rồi, chỉ chạy `sha256sum` để lấy hash của file đó. Hash là chuỗi
64 ký tự đứng trước tên file; lưu lại để dùng ở bước 5. Nén lại có thể đổi hash.

File nằm tại **`/tmp/node-agent-recovery.tar.gz`**. Kiểm tra nội dung:

```bash
tar -tzf /tmp/node-agent-recovery.tar.gz
```

Danh sách phải có `./agent.py`, `./managed.py` và các module đồng cấp. Không để
archive chứa credentials, checkpoint hoặc dữ liệu riêng. Gói này sẽ được phép
tải công khai qua CloudFront ở bước 4.

### 3.2. Upload bằng trình duyệt

1. Mở **S3 → bucket ở bước 1 → tab Objects**.
2. Bấm **Create folder**, đặt tên `agent`, rồi mở thư mục đó.
3. Tạo tiếp thư mục `releases`, mở nó.
4. Tạo tiếp thư mục tên `v1`, mở nó.
5. Bấm **Upload → Add files**, chọn `/tmp/node-agent-recovery.tar.gz`.
6. Bấm **Upload**, đợi trạng thái thành công.

Nếu hộp chọn file khó tìm `/tmp`, copy file sang thư mục dễ tìm trên cùng máy:

```bash
cp /tmp/node-agent-recovery.tar.gz ~/Downloads/node-agent-recovery.tar.gz
```

Lệnh copy cần thư mục `~/Downloads` tồn tại. Với WSL, có thể mở đường dẫn
`\\wsl$` trong Windows Explorer để lấy file từ filesystem của distro. Copy không
đổi hash. Nếu file nằm trên server, tải về máy đang mở trình duyệt trước.

Sau khi upload, object key (đường dẫn bên trong bucket) phải là:

```text
agent/releases/v1/node-agent-recovery.tar.gz
```

**Xong bước 3 khi:** thấy file ở đúng đường dẫn trên trong bucket dùng chung.
Lần phát hành sau dùng thư mục mới như `v2`; không ghi đè file của `v1` đang dùng.

Quyền upload là quyền của **tài khoản đăng nhập Console**, không phải role EC2
ở bước 2. Nếu upload báo AccessDenied, cần quản trị viên cấp `s3:PutObject` trên
`arn:aws:s3:::TEN_BUCKET/agent/releases/*` cho tài khoản/role đăng nhập;
Console cũng có thể cần quyền liệt kê bucket. Không tạo thêm EC2 role để sửa
lỗi upload trên trình duyệt.

## 4. Tạo link tải agent bằng CloudFront

VM mới cần một link HTTPS để tải file agent. CloudFront cung cấp link đó và đọc
file từ `agent/releases/` trong bucket dùng chung. Giữ **Block all public access** bật;
file chương trình vẫn tải công khai được qua link CloudFront. Bucket policy bên
dưới chỉ cho CloudFront đọc `agent/releases/*`, không cấp quyền đọc `checkpoints/*`.

Không dùng nút Copy URL của object private trên S3 làm link cho agent: installer
hiện tải HTTP thông thường, không đăng nhập S3. Link presigned có hạn sử dụng
cũng không phù hợp cho VM có thể được tạo lại nhiều ngày sau.

### 4.1. Tạo distribution trỏ tới bucket dùng chung

1. Mở **CloudFront → Create distribution**.
2. Chọn origin là **Amazon S3**, rồi chọn **bucket dùng chung ở bước 1**.
3. Dùng S3 bucket endpoint, không chọn S3 website endpoint. Để **Origin path** trống.
4. Tạo distribution với domain mặc định `...cloudfront.net`; chưa cần domain riêng.
5. Mở distribution vừa tạo và ghi lại **Distribution ARN** cùng
   **Distribution domain name** ở trang chi tiết.

Ở phần **Behaviors**, chỉnh behavior mặc định: Viewer protocol policy là
**Redirect HTTP to HTTPS**, Allowed methods là **GET, HEAD**, không bật yêu cầu
signed URLs/cookies của viewer. Installer hiện không có bước đăng nhập CloudFront.

### 4.2. Cho CloudFront quyền đọc riêng agent/releases/

**Đây là bucket policy của S3, không phải IAM role ở bước 2.**

1. Vào **S3 → bucket ở bước 1 → Permissions**.
2. Tìm **Bucket policy → Edit**.
3. Với bucket mới chưa có policy, dán JSON sau. Nếu đã có policy, thêm statement
   vào policy hiện có để giữ các quyền đang sử dụng.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFrontReadAgentReleases",
      "Effect": "Allow",
      "Principal": {"Service": "cloudfront.amazonaws.com"},
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::TEN_BUCKET/agent/releases/*",
      "Condition": {
        "StringEquals": {
          "AWS:SourceArn": "DAN_DISTRIBUTION_ARN_VAO_DAY"
        }
      }
    }
  ]
}
```

4. Thay hai giá trị:

   | Giá trị trong JSON | Thay bằng |
   | --- | --- |
   | `TEN_BUCKET` | Tên bucket dùng chung, ví dụ `spulti-sonvt` |
   | `DAN_DISTRIBUTION_ARN_VAO_DAY` | Toàn bộ ARN đã copy ở 4.1, dạng `arn:aws:cloudfront::123456789012:distribution/E123EXAMPLE` |

5. Bấm **Save changes**. Giữ Block Public Access bật.

Resource phải kết thúc bằng `/agent/releases/*`. Không dùng `TEN_BUCKET/*`
vì sẽ cấp CloudFront quyền đọc cả checkpoint trong bucket.

### 4.3. Bật OAC để CloudFront sử dụng quyền trên

OAC là cấu hình để CloudFront xác thực khi đọc bucket private; không phải role
EC2 và bạn không cần tạo thêm IAM role.

1. Vào **CloudFront → Origin access → Create control setting**.
2. Name: `spulti-agent-releases`; Origin type: **S3**.
3. Chọn **Sign requests (recommended)**, bấm **Create**.
4. Mở distribution ở 4.1 → **Origins**, chọn origin S3 → **Edit**.
5. Origin access: **Origin access control settings (recommended)**.
6. Chọn OAC `spulti-agent-releases` vừa tạo → **Save changes**.
7. Chờ distribution triển khai xong, không còn trạng thái Deploying.

Nếu wizard tạo distribution đã tạo và gắn OAC, kiểm tra lại lựa chọn trên,
không cần tạo OAC thứ hai. Xem [hướng dẫn OAC của AWS](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-restricting-access-to-s3.html).

### 4.4. Thử link tải

Ghép **Distribution domain name** ở 4.1 với object key ở 3.2. Ví dụ:

```text
https://d123example.cloudfront.net/agent/releases/v1/node-agent-recovery.tar.gz
```

Thay domain ví dụ bằng domain thật, thử mở link trong trình duyệt: file phải tải
được. Hoặc dùng terminal trên máy cá nhân:

```bash
curl --fail --location \
  'https://DISTRIBUTION_DOMAIN/agent/releases/v1/node-agent-recovery.tar.gz' \
  --output /tmp/node-agent-downloaded.tar.gz
sha256sum /tmp/node-agent-downloaded.tar.gz
```

**Xong bước 4 khi:** tải được file và hash trùng hash ở bước 3.1. Lưu link này
để điền `.env`. VM chạy job cần mạng ra ngoài để tải link này và gọi API.

## 5. Cấu hình control plane và kiểm tra S3

**Từ bước này, SSH vào EC2 control plane.** Mở file `control-plane/.env`
trong repo trên EC2, giữ các giá trị RDS/JWT đã cấu hình và điền thêm:

| Biến | Lấy giá trị ở đâu? |
| --- | --- |
| `SPULTI_CHECKPOINT_BUCKET` | Tên bucket dùng chung ở bước 1; không thêm `s3://` |
| `AWS_REGION` | Region của bucket dùng chung, ví dụ `ap-southeast-1` |
| `SPULTI_PUBLIC_API_URL` | URL HTTPS của API control plane; không phải domain CloudFront tải agent |
| `SPULTI_AGENT_RELEASE_URL` | Link tải đã kiểm tra ở bước 4.4 |
| `SPULTI_AGENT_RELEASE_SHA256` | Chuỗi hash 64 ký tự ở bước 3.1 |

Ví dụ cấu hình (thay các giá trị mẫu bằng giá trị thật):

```dotenv
SPULTI_CHECKPOINT_BUCKET=THAY_TEN_BUCKET
AWS_REGION=ap-southeast-1
SPULTI_PUBLIC_API_URL=https://api.example.com
SPULTI_AGENT_RELEASE_URL=https://DISTRIBUTION_DOMAIN/agent/releases/v1/node-agent-recovery.tar.gz
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

Bucket dùng chung và link tải agent vẫn dùng như trên. `compose.local.yaml` truyền AWS credentials
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

Nếu container API không lấy được role EC2 dù đã gắn role: kiểm tra EC2 metadata
options, bật endpoint, dùng IMDSv2 và response hop limit **2** cho Docker bridge.
Xem [AWS metadata options](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-options.html).
EC2 cũng cần đường mạng tới S3 qua internet/NAT hoặc S3 VPC endpoint.


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
