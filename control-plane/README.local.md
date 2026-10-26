# Chạy Spulti-cloud local trên Windows 11 và Ubuntu

Hướng dẫn dành cho thành viên muốn chạy control plane trên máy cá nhân, không cần SSH vào EC2 và không cần kết nối RDS. API, Celery worker, Redis, Nginx và PostgreSQL chạy bằng Docker Compose; AWS/GCP SDK trong worker vẫn gọi API cloud thật.

**Local ở đây là nơi chạy control plane. Lệnh `create` của adapter vẫn tạo VM Spot trên AWS/GCP và phát sinh chi phí.** `docker compose down` không xóa các VM đó; cần chạy `delete` và kiểm tra trên cloud.

Thông tin GCP của nhóm:

| Thuộc tính | Giá trị |
| --- | --- |
| Project ID | `spulti-cloud` |
| Service account gọi API | `spulti-control-plane@spulti-cloud.iam.gserviceaccount.com` |
| Region / zone đang dùng trong script | `asia-southeast1` / `asia-southeast1-b` |
| Subnet đang dùng trong script | `spulti-test-subnet` |

## 1. Repo cần gì để chạy local?

| Thành phần | Vai trò và cách cài |
| --- | --- |
| AWS CLI v2 (`aws`) | Đăng nhập AWS, chọn profile và lấy credentials cho worker. Cài trên máy cá nhân. |
| Google Cloud CLI (`gcloud`, thuộc Google Cloud SDK) | Đăng nhập Google và tạo credentials cho service account. Cài trên máy cá nhân. |
| Python SDK | `boto3`, `google-cloud-compute`, `google-auth` đã có trong `requirements.txt`; Dockerfile tự cài khi build. Cài CLI không đồng nghĩa đã cài các thư viện này vào Python của IDE. |
| Docker + Compose | Chạy toàn bộ control plane và database local. Windows dùng Linux containers. |
| Python trên host | Chỉ cần nếu muốn IDE nhận thư viện, chạy Python riêng hoặc cài `spulti` CLI; hướng dẫn dùng Python 3.12 giống Dockerfile. |

`compose.yaml` hiện dành cho EC2: mount `/etc/spulti/secrets/gcp-credentials.json`, `/etc/spulti/ssh/spulti-gcp-test.pub`, `/var/lib/spulti-tests` và lấy URL database từ `.env`. Laptop không tự có các file hay IAM role của EC2.

Hướng dẫn này dùng **`compose.local.yaml` độc lập** và **`.env.local`**:

- Database dùng service `postgres`, không dùng RDS hay dữ liệu chung của nhóm.
- Truy cập API qua `http://localhost:8080`; chỉ bind cổng HTTP vào `127.0.0.1`.
- AWS credentials được truyền từ terminal vào worker. GCP credentials được mount từ `control-plane/.local/gcp-credentials.json`.
- Bên trong worker, đường dẫn GCP vẫn là `/run/secrets/gcp-credentials.json`. Không đặt đường dẫn Windows vào `GOOGLE_APPLICATION_CREDENTIALS` của container.
- PostgreSQL, Redis và state thử AWS nằm trong các Docker volume của project `spulti-local`.

Luôn dùng đầy đủ `docker compose --env-file .env.local -f compose.local.yaml ...` trong thư mục `control-plane`. Không ghép `-f compose.yaml -f compose.local.yaml`, vì có thể giữ lại mount/cấu hình EC2.

## 2. Cài công cụ trên Windows 11

Dùng **PowerShell** cho các lệnh Windows trong tài liệu. Không trộn credentials cài ở Windows với terminal WSL; nếu làm hoàn toàn trong WSL Ubuntu, làm theo nhánh Ubuntu và bật Docker Desktop WSL integration cho distro đó.

### Docker, Git và Python

1. Cài [Docker Desktop cho Windows](https://docs.docker.com/desktop/setup/install/windows-install/), chọn backend WSL 2, mở Docker Desktop và chờ engine sẵn sàng. Bật virtualization trong BIOS/UEFI nếu được yêu cầu.
2. Nếu chưa có WSL, chạy `wsl --install` trong PowerShell Administrator, khởi động lại nếu được yêu cầu rồi chạy `wsl --update`.
3. Cài [Git for Windows](https://git-scm.com/downloads/win). Nếu cần Python cho IDE/CLI, cài Python 3.12 từ [python.org](https://www.python.org/downloads/windows/) với Python Launcher và PATH.
4. Mở lại terminal, kiểm tra:

```powershell
git --version
docker version
docker compose version
# Chỉ cần khi dùng Python trên host:
py -3.12 --version
```

### AWS CLI v2

Tải và chạy [AWS CLI v2 MSI 64-bit](https://awscli.amazonaws.com/AWSCLIV2.msi), rồi mở terminal mới:

```powershell
aws --version
```

Kết quả phải bắt đầu bằng `aws-cli/2`. Xem [hướng dẫn cài chính thức của AWS](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).

### Google Cloud CLI

Tải bộ cài Windows từ [trang cài Google Cloud CLI](https://docs.cloud.google.com/sdk/docs/install-sdk), chạy bộ cài và bật tùy chọn thêm `gcloud` vào PATH. Có thể dùng Python đi kèm bộ cài. Mở terminal mới:

```powershell
gcloud --version
```

## 3. Cài công cụ trên Ubuntu

Các lệnh dưới đây dùng Bash, phù hợp Ubuntu 22.04/24.04 LTS. Nếu đã cài Docker từ nguồn khác, xử lý gói xung đột theo [tài liệu Docker](https://docs.docker.com/engine/install/ubuntu/) trước khi cài Engine từ repository chính thức.

### Docker Engine và Compose plugin

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg unzip less groff git openssh-client openssl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

Đăng xuất rồi đăng nhập lại để nhận group `docker`. Group này có quyền tương đương root; chỉ thêm tài khoản phát triển đáng tin cậy. Sau đó chạy các lệnh Compose bằng user thường để giữ được biến môi trường AWS của terminal.

```bash
docker version
docker compose version
```

### AWS CLI v2

Ví dụ cho CPU **x86_64**; kiểm tra bằng `uname -m`. Với ARM64, thay `x86_64` trong URL bằng `aarch64`. Thực hiện trong một thư mục tải xuống trống:

```bash
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip awscliv2.zip
sudo ./aws/install
aws --version
```

Nếu đã có CLI v2, dùng `sudo ./aws/install --update`. Cách kiểm tra chữ ký bộ cài nằm trong [tài liệu cài AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).

### Google Cloud CLI

```bash
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update
sudo apt-get install -y google-cloud-cli
gcloud --version
```

Nếu repository/keyring đã có thì bỏ qua hai lệnh thêm tương ứng. Nguồn: [Google Cloud CLI trên Debian/Ubuntu](https://docs.cloud.google.com/sdk/docs/install-sdk).

## 4. Lấy source và chuẩn bị thư mục local

Nếu đã clone repo, chỉ cần chuyển vào `spulti-cloud/control-plane`. Nếu chưa có:

```text
git clone https://github.com/Sonvt2006/spulti-cloud.git
cd spulti-cloud/control-plane
```

Các lệnh từ đây, trừ khi ghi khác, đều chạy tại **`control-plane`**. Không ghi đè `.env.local` hoặc SSH key đã dùng trước đó.

**Windows 11:**

```powershell
New-Item -ItemType Directory -Force .local | Out-Null
Copy-Item .env.local.example .env.local
$jwtBytes = New-Object byte[] 48
$jwtRng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$jwtRng.GetBytes($jwtBytes)
$jwtRng.Dispose()
$jwtSecret = [Convert]::ToBase64String($jwtBytes)
(Get-Content .env.local) -replace '^JWT_SECRET=.*$', "JWT_SECRET=$jwtSecret" | Set-Content -Encoding ascii .env.local
```

**Ubuntu:**

```bash
mkdir -p .local
chmod 700 .local
cp .env.local.example .env.local
chmod 600 .env.local
sed -i "s/^JWT_SECRET=.*/JWT_SECRET=$(openssl rand -hex 48)/" .env.local
```

`.local/` và `.env.local` được bỏ qua bởi Git và Docker build context. Không gửi credentials vào chat, không commit JSON service account hoặc private key. Mật khẩu PostgreSQL `spulti_local` trong Compose chỉ dành cho database phát triển nội bộ Docker; cấu hình này không dùng để triển khai production.

## 5. Cấu hình AWS cho từng thành viên

### 5.1. Trường hợp đã có IAM user `spulti-dev`

Với cấu hình nhóm đã cung cấp, IAM user `spulti-dev` thuộc group `spulti`, nhận các policy qua group, trong đó có `AdministratorAccess`. Làm theo cách **IAM user + access key** dưới đây; không cần chạy `aws configure sso` hay `aws sso login` cho cách này.

Phân biệt các tên trước khi cấu hình:

| Khái niệm | Trong ví dụ này |
| --- | --- |
| IAM user trên AWS | `spulti-dev`: danh tính mà AWS dùng để kiểm tra quyền. |
| IAM group | `spulti`: nhóm gán quyền cho các user thành viên. Không phải tên đăng nhập hay credentials. |
| Access key ID + secret access key | Cặp thông tin cho CLI/SDK xác thực dưới danh tính IAM user. Khác mật khẩu đăng nhập AWS Console. |
| Profile trên máy | `spulti-dev`: tên bộ cấu hình lưu cục bộ, tự đặt. Chọn trùng tên user cho dễ nhớ, nhưng không bắt buộc. |

`AdministratorAccess` cho phép toàn bộ action/resource theo policy, nên ảnh hiện tại không cho thấy cần gắn thêm policy Allow để thử EC2. Quyền thực tế vẫn có thể bị giới hạn bởi SCP, permissions boundary hoặc explicit Deny. Đây là quyền rất rộng; không dùng chung access key của user này cho cả nhóm. Xem [policy AdministratorAccess](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AdministratorAccess.html).

**Bước A — Lấy access key của đúng IAM user:**

1. AWS Console → **IAM → Users → spulti-dev** (chọn user, không phải group).
2. Mở tab **Security credentials**, tìm **Access keys → Create access key**.
3. Chọn mục đích **Command Line Interface (CLI)**, đọc và xác nhận hướng dẫn của AWS; tiếp tục tạo key, có thể đặt mô tả như `long-local`.
4. Lưu **Access key ID** và **Secret access key** vào nơi riêng an toàn. Secret chỉ hiển thị khi tạo; không gửi vào chat hay commit vào repo. Nếu đã có cặp key còn hiệu lực được lưu an toàn, có thể dùng cặp đó, không cần tạo mới.

Nếu không tạo được key, kiểm tra quyền tạo key hoặc nhờ quản trị viên. Không xóa một key đang dùng chỉ để tạo key khác. Nguồn: [quản lý access key của IAM user](https://docs.aws.amazon.com/IAM/latest/UserGuide/access-key-self-managed.html).

**Bước B — Lưu vào profile trên laptop:**

Mở PowerShell trên Windows hoặc Bash trên Ubuntu, chạy ở thư mục bất kỳ:

```text
aws configure --profile spulti-dev
```

Điền bốn giá trị khi được hỏi (phần trong `<...>` phải thay bằng key thật, không gõ nguyên văn):

```text
AWS Access Key ID [None]: <access key ID vừa tạo>
AWS Secret Access Key [None]: <secret access key vừa tạo>
Default region name [None]: us-east-1
Default output format [None]: json
```

CLI lưu profile ở `%USERPROFILE%\.aws\credentials` và `%USERPROFILE%\.aws\config` trên Windows, hoặc `~/.aws/credentials` và `~/.aws/config` trên Ubuntu. Không cần chép key vào `.env.local`. Lệnh này cấu hình máy cá nhân, không tạo IAM user mới trên AWS. Xem [AWS CLI với IAM user](https://docs.aws.amazon.com/cli/latest/userguide/cli-authentication-user.html).

Access key dài hạn của IAM user không có session token. Nếu dùng lại một profile từng chứa credentials tạm thời, bỏ trường `aws_session_token` cũ trong đúng profile đó; không xóa các profile khác.

**Bước C — Kiểm tra laptop đang dùng đúng danh tính:**

```text
aws sts get-caller-identity --profile spulti-dev
```

Kết quả có `Account` là AWS account ID và `Arn` dạng `arn:aws:iam::<ACCOUNT_ID>:user/spulti-dev` (có thể có IAM path trước tên user). Đối chiếu với user/account vừa chọn. Lệnh này không tạo VM và không in secret.

Đến đây laptop đã xác thực được với AWS. **Worker trong Docker chưa tự nhận profile này**: tiếp tục mục 5.2 để truyền credentials vào container. IAM role gắn trên máy EC2 cũng không tự có hiệu lực trên laptop.

**Khi thêm thành viên:** mỗi người nên có IAM user riêng và access key riêng, được cấp quyền phát triển phù hợp. Ví dụ user `spulti-an` trên AWS vẫn có thể đặt profile trên máy An là `spulti-dev` để dùng nguyên các lệnh trong tài liệu; `get-caller-identity` khi đó sẽ trả user `spulti-an`. Không tự động thêm mọi thành viên vào group `spulti` hiện có quyền quản trị; quản trị viên cần chọn phạm vi quyền phù hợp.

<details>
<summary>Cách khác: chỉ dùng khi quản trị viên đã thiết lập IAM Identity Center (SSO)</summary>

Thay bước A–C bằng các lệnh sau và dùng SSO start URL, SSO region, account/role do quản trị viên cung cấp:

```text
aws configure sso --profile spulti-dev
aws sso login --profile spulti-dev
aws sts get-caller-identity --profile spulti-dev
```

Dùng một profile riêng nếu muốn giữ cả cấu hình IAM user và SSO, rồi thay tên profile tương ứng trong các lệnh export. SSO dùng credentials tạm thời; xem [cấu hình IAM Identity Center](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).

</details>

### 5.2. Truyền credentials vào Docker worker

Chạy trong **cùng terminal sẽ dùng Compose**, mỗi khi mở terminal mới, đổi key hoặc credentials tạm thời hết hạn. Đoạn dưới lấy credentials từ profile nhưng không in secret ra terminal:

**Windows 11:**

```powershell
$awsJson = aws configure export-credentials --profile spulti-dev --format process
if ($LASTEXITCODE -ne 0) { throw 'Không lấy được AWS credentials' }
$awsCreds = $awsJson | ConvertFrom-Json
$env:AWS_ACCESS_KEY_ID = $awsCreds.AccessKeyId
$env:AWS_SECRET_ACCESS_KEY = $awsCreds.SecretAccessKey
$env:AWS_SESSION_TOKEN = $awsCreds.SessionToken
$env:AWS_DEFAULT_REGION = 'us-east-1'
Remove-Variable awsJson, awsCreds
```

**Ubuntu:**

```bash
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
aws_exports=$(aws configure export-credentials --profile spulti-dev --format env) && eval "$aws_exports"
unset aws_exports
export AWS_DEFAULT_REGION=us-east-1
```

Chỉ tiếp tục nếu export thành công. CLI v2 hỗ trợ [export credentials từ profile](https://docs.aws.amazon.com/cli/latest/reference/configure/export-credentials.html), bao gồm profile SSO. Compose local truyền chúng qua environment; không cần mount toàn bộ `~/.aws` hoặc cài CLI trong container.

Credentials tạm thời trong environment container **không tự làm mới**. Sau khi đăng nhập/export lại, cập nhật worker bằng:

```text
docker compose --env-file .env.local -f compose.local.yaml up -d --force-recreate worker
```

`restart worker` không cập nhật environment. Người có quyền quản trị Docker có thể đọc environment container; không chia sẻ output `docker inspect` hay `docker compose config` có chứa credentials. Dùng `config --quiet` khi chỉ kiểm tra cú pháp.

Nếu worker đã chạy, kiểm tra danh tính bên trong container (nếu chưa triển khai stack, làm bước 6–8 trước):

```text
docker compose --env-file .env.local -f compose.local.yaml exec worker python -c "import boto3; r=boto3.client('sts', region_name='us-east-1').get_caller_identity(); print(r['Account']); print(r['Arn'])"
```

Account/Arn phải khớp danh tính đã kiểm tra trên host. Chuỗi cấu hình là: **IAM user → access key → profile trên laptop → biến môi trường terminal → Docker worker → Boto3 gọi AWS**.

## 6. Cấu hình GCP với service account của nhóm

Email `spulti-control-plane@spulti-cloud.iam.gserviceaccount.com` chỉ định danh service account, không phải mật khẩu hay credential có thể dùng trực tiếp. Chọn **một** trong hai cách dưới đây.

### Cách A: Impersonation cho local (khuyến nghị)

Mỗi thành viên đăng nhập Google bằng tài khoản riêng, sau đó SDK gọi API dưới danh tính service account. Python hỗ trợ loại Application Default Credentials (ADC) này. `gcloud auth login` và ADC cho Python là hai cấu hình riêng. Nguồn: [thiết lập ADC local](https://docs.cloud.google.com/docs/authentication/set-up-adc-local-dev-environment).

**Quản trị viên chuẩn bị một lần:**

- Project có billing và Compute Engine API; bật IAM Service Account Credentials API cho impersonation.
- Cấp `roles/iam.serviceAccountTokenCreator` cho tài khoản thành viên **trên service account nói trên**.
- Service account cần quyền thao tác Compute Engine. `roles/compute.instanceAdmin.v1` là vai trò có sẵn để quản lý instance; quản trị viên nên giới hạn theo môi trường phát triển, đồng thời kiểm tra quyền subnet, external IP và image. Với Shared VPC có thể cần `roles/compute.networkUser` trên subnet thuộc host project. Xem [quyền Compute Engine](https://docs.cloud.google.com/compute/docs/access/iam).

Ví dụ lệnh **do quản trị viên chạy**, thay `member@example.com` bằng email thật:

```text
gcloud services enable compute.googleapis.com iamcredentials.googleapis.com --project=spulti-cloud
gcloud iam service-accounts add-iam-policy-binding spulti-control-plane@spulti-cloud.iam.gserviceaccount.com --project=spulti-cloud --member="user:member@example.com" --role="roles/iam.serviceAccountTokenCreator"
```

Quyền Token Creator cho phép impersonate; không tự cấp thêm quyền Compute cho service account. Tham khảo [service account impersonation](https://docs.cloud.google.com/iam/docs/service-account-impersonation).

**Mỗi thành viên chạy trên máy cá nhân** (cú pháp giống nhau trên PowerShell và Bash):

```text
gcloud auth login
gcloud config set project spulti-cloud
gcloud auth application-default login --impersonate-service-account=spulti-control-plane@spulti-cloud.iam.gserviceaccount.com
```

Copy file ADC vừa tạo vào vị trí Compose sẽ mount. Các đường dẫn sau là mặc định; nếu đã đặt `CLOUDSDK_CONFIG`, dùng đường dẫn file mà lệnh login thông báo.

**Windows 11:**

```powershell
Copy-Item "$env:APPDATA\gcloud\application_default_credentials.json" .local/gcp-credentials.json
```

**Ubuntu:**

```bash
cp "$HOME/.config/gcloud/application_default_credentials.json" .local/gcp-credentials.json
chmod 644 .local/gcp-credentials.json
```

Worker chạy bằng UID `10001`, nên file bind mount phải đọc được bởi UID này. Trên Ubuntu, `.local` đã đặt `700` để user khác trên host không truy cập, còn file bên trong dùng `644` để worker đọc được sau khi bind mount. Chỉ áp dụng cách này trong thư mục riêng đã bảo vệ. Trên Windows, giữ quyền thư mục cho tài khoản cá nhân và cho phép Docker Desktop truy cập ổ đĩa chứa repo.

Đây là bản copy ADC, vẫn chứa thông tin xác thực nhạy cảm. SDK tự làm mới access token khi nguồn credentials còn hợp lệ. Nếu đăng nhập ADC lại, copy lại file, đặt lại quyền trên Ubuntu và recreate worker. Khi lỗi quota project, nhờ quản trị viên kiểm tra `serviceusage.services.use` cho danh tính liên quan rồi chạy `gcloud auth application-default set-quota-project spulti-cloud`, copy lại ADC.

### Cách B: Dùng JSON key nếu nhóm đã cấp

Nếu quản trị viên cấp JSON key của đúng service account, copy vào **`control-plane/.local/gcp-credentials.json`** thay cho ADC ở cách A; không cần `gcloud auth activate-service-account` để Python dùng file này. Đối chiếu trường `client_email` với email service account ở đầu tài liệu.

Nếu cần cấp key mới, quản trị viên dùng Google Cloud Console → IAM & Admin → Service Accounts → chọn service account → Keys → Add key → Create new key → JSON, nếu chính sách tổ chức cho phép. Không tắt chính sách cấm key để làm theo hướng dẫn này; dùng impersonation. Nguồn: [tạo và quản lý service account key](https://docs.cloud.google.com/iam/docs/keys-create-delete).

Trên Ubuntu, cũng đặt `chmod 700 .local` và `chmod 644 .local/gcp-credentials.json`. Không commit, gửi chung qua nhóm chat hoặc tái sử dụng key production.

## 7. Tạo SSH public key cho script thử GCP

Script `app/scripts/try_gcp_adapter.py` đọc `/app-keys/spulti-gcp-test.pub` ngay cả khi chạy `status` hoặc `delete`, nên cần tạo file này trước khi khởi động worker.

**Windows 11:**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.ssh" | Out-Null
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\spulti-gcp-local" -C 'spulti-local'
Copy-Item "$env:USERPROFILE\.ssh\spulti-gcp-local.pub" .local/spulti-gcp-test.pub
```

Nếu thiếu `ssh-keygen`, cài **OpenSSH Client** trong Windows Settings → Optional features. Nếu key đã có, bỏ qua lệnh tạo và chỉ copy `.pub`.

**Ubuntu:**

```bash
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
ssh-keygen -t ed25519 -f "$HOME/.ssh/spulti-gcp-local" -C 'spulti-local'
cp "$HOME/.ssh/spulti-gcp-local.pub" .local/spulti-gcp-test.pub
chmod 644 .local/spulti-gcp-test.pub
```

Đặt passphrase khi được hỏi. Private key giữ ở `~/.ssh`; chỉ public key được mount vào worker. Việc tạo VM không yêu cầu mở SSH; nếu cần SSH vào VM, cấu hình firewall theo IP của mình và kiểm tra chính sách OS Login. Adapter hiện dùng SSH key trong instance metadata.

## 8. Khởi động control plane local

Sau khi hoàn thành credentials và SSH public key, chạy trong `control-plane`, ở terminal đã export AWS credentials:

```text
docker compose --env-file .env.local -f compose.local.yaml config --quiet
docker compose --env-file .env.local -f compose.local.yaml build
docker compose --env-file .env.local -f compose.local.yaml up -d --wait postgres redis
docker compose --env-file .env.local -f compose.local.yaml run --rm --no-deps api python -m app.init_db
docker compose --env-file .env.local -f compose.local.yaml run --rm --no-deps --user root worker chown 10001:10001 /var/lib/spulti-tests
docker compose --env-file .env.local -f compose.local.yaml up -d --wait
docker compose --env-file .env.local -f compose.local.yaml ps
```

Lệnh `chown` chỉ chuẩn bị quyền ghi volume state cho worker UID `10001`, cần làm lần đầu hoặc khi tạo lại volume. Các service API/worker vẫn chạy bằng user của Dockerfile.

Mở:

- [Healthcheck](http://localhost:8080/health): dự kiến `{"status":"ok","service":"spulti-api"}`.
- [Swagger UI](http://localhost:8080/docs): thử API đăng ký, đăng nhập và task.

Healthcheck chỉ chứng minh API phản hồi. Để kiểm tra cả database và worker, trong Swagger UI:

1. `POST /api/auth/register`: nhập `name`, email hợp lệ, `username` từ 3 ký tự và password từ 8 ký tự. Đây là tài khoản trong DB local, khác tài khoản AWS/Google.
2. `POST /api/auth/login`: dùng username/password vừa tạo, copy `access_token`.
3. Nhấn **Authorize**, nhập token vào HTTP Bearer, không thêm tiền tố `Bearer` nếu UI đã tự thêm.
4. `POST /api/tasks/demo` với `{"a":15,"b":27}`; nhận `task_id`.
5. Sau khoảng 5 giây, `GET /api/tasks/{task_id}` phải có `status: SUCCESS` và `result.result: 42`.

Để nạp dữ liệu mẫu tùy chọn, đọc `app/seed.py` trước rồi chạy `docker compose --env-file .env.local -f compose.local.yaml run --rm --no-deps api python -m app.seed`.

### Kiểm tra SDK và credentials ngay trong worker

Các lệnh sau giống nhau trên PowerShell/Bash, không tạo VM:

```text
docker compose --env-file .env.local -f compose.local.yaml exec worker python -c "import boto3; from google.cloud import compute_v1; print('AWS/GCP SDK OK')"
docker compose --env-file .env.local -f compose.local.yaml exec worker python -c "import boto3; r=boto3.client('sts', region_name='us-east-1').get_caller_identity(); print(r['Account']); print(r['Arn'])"
docker compose --env-file .env.local -f compose.local.yaml exec worker python -c "from google.cloud import compute_v1; c=compute_v1.InstancesClient(); next(iter(c.list(project='spulti-cloud', zone='asia-southeast1-b')), None); print('GCP read OK')"
```

AWS CLI hoạt động trên host chưa chứng minh worker đã có credentials; phải kiểm tra trong worker như trên. GCP `list` thành công chỉ xác nhận quyền đọc, chưa chứng minh đủ quyền tạo/xóa VM hoặc còn quota Spot.

## 9. Thử tạo, xem và xóa Spot instance

Đây là bước tùy chọn có sử dụng tài nguyên cloud thật. Hai script thử adapter chạy độc lập, không ghi instance vào DB local của ứng dụng.

### AWS

Đọc và chỉnh `app/scripts/try_aws_adapter.py`, hàm `new_state()`, trước khi chạy:

| Cấu hình | Yêu cầu |
| --- | --- |
| `region`, `spec.zone` | Mặc định `us-east-1`, `us-east-1c`; subnet phải thuộc đúng zone. |
| `subnet_id`, `security_group_ids` | ID hiện tại thuộc môi trường của người viết; xác nhận tồn tại trong account của mình và cùng VPC. |
| `key_name` | EC2 key pair `spulti-key` phải tồn tại ở region đó; private key chỉ cần khi SSH. |
| `spec.image_id` | Kiểm tra AMI tồn tại ở region đã chọn, tương thích loại máy và kích thước root disk. |

Theo các lệnh trong adapter, identity cần quyền như `ec2:DescribeImages`, `ec2:RunInstances`, `ec2:CreateTags`, `ec2:DescribeInstances`, `ec2:TerminateInstances`; quản trị viên cần kiểm tra policy theo tài nguyên cụ thể, quyền KMS nếu áp dụng và service-linked role cho Spot. Các quyền này không phải policy IAM đầy đủ cho mọi chức năng của repo.

Sau khi sửa script, build lại image rồi thực hiện:

```text
docker compose --env-file .env.local -f compose.local.yaml up -d --build worker
docker compose --env-file .env.local -f compose.local.yaml exec worker python -m app.scripts.try_aws_adapter create
docker compose --env-file .env.local -f compose.local.yaml exec worker python -m app.scripts.try_aws_adapter status
docker compose --env-file .env.local -f compose.local.yaml exec worker python -m app.scripts.try_aws_adapter delete
```

Xác nhận instance đã `terminated` bằng `status` hoặc EC2 Console. File `/var/lib/spulti-tests/aws-adapter-test.json` trong volume giữ cấu hình và ID instance qua các lần restart. `delete` không xóa state; sửa source cũng không thay cấu hình đã lưu. Chỉ sau khi xác nhận VM cũ đã bị terminate và không cần state để truy vết, bắt đầu vòng test mới bằng:

```text
docker compose --env-file .env.local -f compose.local.yaml exec worker python -c "from pathlib import Path; Path('/var/lib/spulti-tests/aws-adapter-test.json').unlink(missing_ok=True)"
```

Nếu tạo máy lỗi giữa chừng, đối chiếu EC2 Console theo tên/request ID trong state trước khi xóa state hoặc thử lại.

### GCP

Trước lần đầu, sửa `app/scripts/try_gcp_adapter.py`:

```python
# Ví dụ: thay an bằng định danh riêng của bạn, tên dùng chữ thường/số/dấu gạch ngang.
INSTANCE_NAME = "spulti-gcp-an-01"
CREATE_GENERATION = "01"
USER = "an"
```

**Phải đổi cả `INSTANCE_NAME`**: chỉ đổi `USER` không tách VM của các thành viên. Lệnh `status` và `delete` xác định VM bằng project/zone/`INSTANCE_NAME`. Giữ nguyên ba giá trị trong cả vòng `create → status → delete`; khi tạo vòng mới, tăng generation, và có thể dùng tên mới. Khi retry cùng một yêu cầu đang dang dở, giữ cấu hình và generation.

Kiểm tra subnet và image đang hard-code trước khi tạo:

```text
gcloud compute networks subnets describe spulti-test-subnet --region=asia-southeast1 --project=spulti-cloud --impersonate-service-account=spulti-control-plane@spulti-cloud.iam.gserviceaccount.com
gcloud compute images describe-from-family ubuntu-2204-lts --project=ubuntu-os-cloud --format="value(selfLink)"
```

Lệnh đầu dùng cách A và quyền đọc subnet; nếu dùng JSON key thì kiểm tra bằng Console hoặc identity CLI được quản trị viên cấp. Lệnh image dùng danh tính CLI hiện tại. Nếu image trong source không còn dùng được, lấy `selfLink` từ image phù hợp và cập nhật `spec.image_id`. Những giá trị subnet/image trong repo không được hướng dẫn này xác nhận là còn tồn tại trên cloud.

```text
docker compose --env-file .env.local -f compose.local.yaml up -d --build worker
docker compose --env-file .env.local -f compose.local.yaml exec worker python -m app.scripts.try_gcp_adapter create
docker compose --env-file .env.local -f compose.local.yaml exec worker python -m app.scripts.try_gcp_adapter status
docker compose --env-file .env.local -f compose.local.yaml exec worker python -m app.scripts.try_gcp_adapter delete
```

Sau khi xóa, `status` có thể trả `NotFound`; kiểm tra thêm Compute Engine Console. Khi tạo bị timeout, tra VM theo đúng tên trước khi tạo lại. Service account trong tài liệu dùng để **worker gọi API**; adapter hiện không cấu hình `service_accounts` gắn vào VM mới.

## 10. Cài Python SDK trên host cho IDE và Spulti CLI (tùy chọn)

Không cần bước này nếu chỉ chạy Docker. Các script adapter đang dùng đường dẫn Linux cố định, vì vậy chạy chúng bằng `docker compose exec worker` như trên, kể cả khi đã cài SDK trên Windows.

Từ **thư mục gốc repo**:

**Windows 11:**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r control-plane/requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ./cli
.\.venv\Scripts\python.exe -c "import boto3; from google.cloud import compute_v1; print('SDK OK')"
.\.venv\Scripts\spulti.exe login --api-url http://localhost:8080
.\.venv\Scripts\spulti.exe whoami
```

**Ubuntu 24.04** (Python 3.12 trong repository hệ điều hành):

```bash
sudo apt-get install -y python3.12 python3.12-venv
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r control-plane/requirements.txt
python -m pip install -e ./cli
python -c "import boto3; from google.cloud import compute_v1; print('SDK OK')"
spulti login --api-url http://localhost:8080
spulti whoami
```

Ubuntu 22.04 mặc định có Python 3.10, trong khi repo dùng tính năng Python 3.11 trở lên. Có thể bỏ qua bước cài host và dùng Python 3.12 trong Docker; nếu cần IDE host, cài Python 3.12 theo phương thức nhóm quản lý trước khi chạy các lệnh venv. Không thay thế Python hệ thống.

Chọn interpreter `.venv` trong IDE. `requirements.txt` cài cả dependencies ứng dụng; nếu chỉ thử thư viện độc lập thì dùng `python -m pip install boto3 google-cloud-compute google-auth`. Xem thêm [Boto3 quickstart](https://docs.aws.amazon.com/boto3/latest/guide/quickstart.html).

Đăng ký tài khoản local ở bước 8 trước khi `spulti login`. Nếu chạy một đoạn Python gọi cloud trực tiếp trên host, dùng AWS profile `spulti-dev` hoặc các biến đã export; với GCP, đặt `GOOGLE_APPLICATION_CREDENTIALS` thành đường dẫn tuyệt đối tới file `.local/gcp-credentials.json` trên host (PowerShell: `$env:GOOGLE_APPLICATION_CREDENTIALS = (Resolve-Path control-plane/.local/gcp-credentials.json).Path`; Bash từ repo root: `export GOOGLE_APPLICATION_CREDENTIALS="$PWD/control-plane/.local/gcp-credentials.json"`).

## 11. Vận hành và xử lý lỗi

Quay về `control-plane` trước khi chạy:

```text
docker compose --env-file .env.local -f compose.local.yaml logs --tail=100 api worker
docker compose --env-file .env.local -f compose.local.yaml up -d --build
docker compose --env-file .env.local -f compose.local.yaml down
```

Source được copy lúc build, không mount trực tiếp; sửa Python cần `up -d --build`. Trước mỗi lần `up` có thể recreate worker, bảo đảm terminal vẫn có AWS credentials. `down` giữ dữ liệu volume; tránh `down -v` khi còn cần DB hoặc state truy vết VM.

| Triệu chứng | Cách xử lý |
| --- | --- |
| Docker không kết nối engine | Mở Docker Desktop và chọn Linux containers trên Windows; kiểm tra Docker service/group trên Ubuntu. |
| Mount vẫn nhắc `/etc/spulti/...` | Đang dùng nhầm `compose.yaml`. Dùng riêng `-f compose.local.yaml`. |
| `bind source path does not exist` | Tạo đủ `.local/gcp-credentials.json` và `.local/spulti-gcp-test.pub`; mount cố ý không tự tạo thư mục thay cho file. |
| `Permission denied` với JSON/public key | Kiểm tra quyền file cho UID `10001`; Ubuntu dùng thư mục `.local` `700`, hai file mount `644`. Không đổi private SSH key thành `644`. |
| `Permission denied` với AWS state | Chạy lại lệnh `chown` volume ở bước 8. |
| `NoCredentialsError` / `ExpiredToken` | Đăng nhập AWS, export lại trong terminal hiện tại và `up -d --force-recreate worker`. |
| GCP `DefaultCredentialsError` / `invalid_grant` | Kiểm tra file đúng loại, login ADC và copy lại nếu phiên đã hết hiệu lực; recreate worker. |
| GCP `iam.serviceAccounts.getAccessToken` bị từ chối | Kiểm tra Token Creator trên đúng service account và IAM Credentials API. |
| GCP/AWS `403`, `AccessDenied` khi tạo VM | Xác thực thành công chưa đủ; kiểm tra quyền tài nguyên, org policy, subnet/image và quota. |
| GCP `AlreadyExists` | Kiểm tra `INSTANCE_NAME` có trùng máy của người khác; không gọi `delete` cho tới khi xác nhận đúng máy của mình. |
| AWS invalid subnet/SG/AMI/key pair | Sửa ID theo đúng account/region/zone; kiểm tra state cũ đang được tái sử dụng. |
| `connection refused` tới DB hoặc `relation does not exist` | Chờ `postgres` healthy, chạy `app.init_db`; không dùng URL RDS hoặc `localhost` cho DB bên trong container. |
| Cổng `8080` đang dùng | Đổi `LOCAL_HTTP_PORT` trong `.env.local`, chạy `up -d` và cập nhật URL API/CLI. |
| Task không thoát `PENDING` | Kiểm tra worker/Redis và log; `/health` không kiểm tra worker. |

Chạy xong một vòng thử cloud: xác nhận VM đã xóa/terminate, kiểm tra tài nguyên còn sót trên console rồi mới dừng stack. Cài SDK và xác thực không tự tạo subnet, security group, key pair hoặc cấp IAM cho thành viên.
