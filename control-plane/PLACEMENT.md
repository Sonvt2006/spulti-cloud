# Placement Engine

Engine đọc catalog, offerings, lịch sử giá và rủi ro trong DB để đề xuất máy.
Không gọi cloud API, tạo VM hay giữ chỗ quota. Không cần thay đổi schema.

## API

`POST /api/placement/recommendations` yêu cầu JWT hoặc API key:

```bash
curl -X POST http://localhost/api/placement/recommendations \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"vcpu":4,"ram_gib":16,"gpu_count":0,"region":"singapore","optimization_mode":"BALANCED","limit":10}'
```

Trả danh sách ứng viên đã xếp hạng, điểm thấp nhất đứng đầu; không có ứng viên
thì trả `200 []`. Dữ liệu không hợp lệ trả `422`; chưa đăng nhập trả `401`.
Các số Decimal được JSON serialize thành chuỗi để giữ độ chính xác.

Các trường lọc: `vcpu`, `ram_gib` (bắt buộc, dương), `gpu_count` (mặc định 0),
`provider` (`AWS`/`GCP`), `region`, `zone`, `optimization_mode` và `limit` (1–100).
CPU/RAM/GPU là yêu cầu tối thiểu. `region=singapore` ánh xạ AWS `ap-southeast-1`
và GCP `asia-southeast1`; có thể dùng tên region cụ thể. Bỏ region để tìm ở tất cả
offerings. Chỉ dùng zone đã có trong catalog. Catalog chưa có loại GPU, nên
`gpu_type` khác null bị từ chối. Dung lượng disk/image do bước tạo máy xử lý.

Chọn nhiều vị trí bằng `providers` (danh sách `AWS`/`GCP`), `regions` (danh sách
`{"provider":"AWS","region":"ap-southeast-1"}`) và `zones` (danh sách
`{"provider":"AWS","region":"ap-southeast-1","zone":"ap-southeast-1a"}`).
Mỗi danh sách cho phép nhiều mục; các nhóm lọc kết hợp với nhau. Danh sách trống
không giới hạn nhóm đó. Các trường đơn cũ vẫn được hỗ trợ và kết hợp với bộ lọc
mới nếu cùng gửi. Bộ lọc áp dụng trước khi xếp hạng và giới hạn số đề xuất,
đồng thời được lưu khi tạo máy để giữ nguyên phạm vi khi khôi phục.

Dashboard dùng checkbox độc lập, yêu cầu chọn ít nhất một nhà cung cấp; chỉ hiện
region thuộc nhà cung cấp đã tick và AZ thuộc region đã tick. Bỏ tick mục cha
sẽ bỏ các lựa chọn con tương ứng. Không tick region/AZ nghĩa là không giới hạn
thêm ở cấp đó.

## Quy tắc xếp hạng

1. Lọc cấu hình, nhà cung cấp và vị trí; loại ứng viên trong danh sách loại trừ
   còn hiệu lực hoặc bị quota callback từ chối.
2. Lấy giá mới nhất có thời điểm hiệu lực không vượt quá thời điểm đánh giá.
   Ưu tiên giá đúng zone, sau đó giá region (`zone=NULL`); không mượn giá zone khác.
   Thiếu giá spot thì loại; thiếu giá on-demand vẫn xếp hạng, tiết kiệm trả null.
   Giá on-demand bằng 0 cũng trả tiết kiệm null. Giá spot bằng 0 hợp lệ.
3. Lấy rủi ro mới nhất theo cùng quy tắc zone/region. Dùng cận trên của bucket;
   bucket mở như AWS `>20%` dùng điểm rủi ro 1. Thiếu rủi ro dùng mặc định 0.5.
4. Chế độ `SAVING` không loại ứng viên theo rủi ro, kể cả khi thiếu dữ liệu rủi ro;
   ưu tiên xếp hạng theo giá Spot thấp nhất.
5. Chuẩn hóa min-max giá và rủi ro trên toàn bộ ứng viên còn lại, rồi tính
   `score = w * normalized_price + (1-w) * normalized_risk`.
   `w=1` cho `SAVING`, `0` cho `STABLE`, `0.5` cho `BALANCED`.
   Nếu mọi giá trị bằng nhau, giá trị chuẩn hóa bằng 0.
6. Phá hòa theo rủi ro, giá, provider/region/zone/tên máy; chỉ áp dụng limit sau
   khi xếp hạng để kết quả ổn định.

Kết quả gồm cấu hình, vị trí, giá spot/on-demand, phần trăm tiết kiệm, điểm,
nguồn/kỳ đo rủi ro, thời điểm dữ liệu và `quota_checked`.
AWS Advisor 30 ngày và GCP preemption hằng ngày khác kỳ đo; điểm rủi ro hiện
là heuristic để xếp hạng, không phải xác suất thu hồi so sánh trực tiếp.
Engine chưa áp dụng TTL: giá có thể có hiệu lực lâu dù không có bản ghi mới.
Collector/vận hành cần đảm bảo dữ liệu được cập nhật; timestamp được trả để kiểm tra.

## Tích hợp Instance Manager / Recovery

```python
from app.services.placement import PlacementRequest, rank_candidates

candidates = rank_candidates(
    db,
    PlacementRequest(vcpu=4, ram_gib=16, region="singapore"),
    exclusions={
        # (provider, region, zone, machine_type): thời điểm UTC hết loại trừ
        ("AWS", "ap-southeast-1", "ap-southeast-1a", "m6i.xlarge"): expires_at,
    },
    quota_available=has_quota,  # callable(candidate) -> bool
)
```

Caller quản lý lưu trữ exclusions và kiểm tra quota; engine không commit DB.
Callback phải kiểm tra quota cho cấu hình của candidate, lỗi callback được truyền
lên caller. Chưa có dịch vụ quota trong repo nên API đề xuất trả
`quota_checked=false`; kết quả chưa bảo đảm có thể cấp máy.
Worker dùng thứ tự trả về để thử ứng viên kế tiếp khi cloud báo thiếu capacity;
việc tạo máy/retry nằm ở Instance Manager, không nằm trong engine.

## Kiểm thử

Từ thư mục `control-plane`, sau khi cài `requirements-test.txt`:

```bash
python -m unittest discover -s tests -v
```

Tests dùng SQLite tạm và không tạo tài nguyên cloud.
