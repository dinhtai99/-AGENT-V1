# WORKFLOW — Quy trình 5 bước của AGENT-V1 (Baito Japan Small Business Lead Finder)

## Bước 1. Nhận lệnh
Đọc lệnh JSON (`task-baito-nhom-da-tham-gia.json`). Kiểm tra `task_id`, `action` (`search_group_posts`).

## Bước 2. Xác định các nhóm Baito đã tham gia
- Nếu lệnh bật `discover_joined_groups: true` (hoặc không truyền `group_ids` cố định), mở trang `https://www.facebook.com/groups/joins/` bằng trình duyệt đã đăng nhập để lấy danh sách các nhóm mà tài khoản đang tham gia.
- Lọc giữ lại các nhóm khớp `group_keywords` (nhóm Baito, việc làm thêm, tuyển dụng tại Nhật: Tokyo, Osaka, Saitama, Chiba, Nagoya, Fukuoka...).
- Quét lần lượt từng nhóm, nghỉ `group_pause_seconds` (mặc định 20 giây) giữa 2 nhóm.

## Bước 3. Đọc bài đăng & Mở rộng nội dung
- Mở trang feed của từng nhóm Baito (`https://www.facebook.com/groups/<group_id>/`).
- Chỉ nhận bài viết có permalink thật (`/permalink/` hoặc `/posts/`).
- Tự động bấm nút `"Xem thêm"` / `"See more"` / `"さらに表示"` trên bài viết dài để lấy trọn vẹn thông tin liên hệ (SĐT, Line, Zalo, địa chỉ ga tàu) ở cuối bài.
- Bóc tách thông tin cá nhân người đăng từ DOM: `author_name`, `author_url` (link trang cá nhân FB), và `group_member_url` (link xem toàn bộ bài người đó đăng trong nhóm).

## Bước 4. Phân loại Chủ kinh doanh nhỏ (< 20 người) & Phân tích 2 bước
Với mỗi bài viết, áp dụng `filters.py`:
1. **Phân loại:** Giữ lại bài của chủ quán / cá nhân kinh doanh nhỏ (`small_business_owner`, quy mô `< 20` người); loại bỏ bài của người đi tìm việc (`job_seeker`) và công ty môi giới/haken tuyển số lượng lớn (`agency_or_large`).
2. **Bước 1 — Xem họ đang kinh doanh gì:** Xác định ngành nghề (`business_type`: Quán ăn Việt, Izakaya/Ramen/Yakiniku, Tiệm Nail/Spa, Tạp hóa thực phẩm Việt, Cafe/Tiệm bánh, Xưởng Bento nhỏ, Shop Online...), tên quán (`shop_name`), ga tàu & khu vực (`location`, `stations`), quy mô (`scale_estimate`).
3. **Bước 2 — Đi sâu tìm hiểu họ cần gì:** Bóc tách vị trí đang thiếu (`roles_needed`), ca làm việc (`shifts_needed`), mức lương (`wage`), tệp khách của quán (`shop_customer_segment`), và các nỗi đau vận hành (`pain_points_and_needs`).

## Bước 5. Lập danh sách Chủ kinh doanh & Trả kết quả
- Trả về `result.json` theo `task-schema.json` (gồm danh sách `posts` và danh sách `leads`).
- Tự động gom nhóm (deduplicate) theo từng chủ quán và cập nhật vào **`leads.json`** cùng bảng tính **`leads.csv`** để người dùng có ngay danh sách tổng hợp từng chủ quán đang kinh doanh gì và cần gì.
