# WORKFLOW — Quy trình 5 bước của AGENT-V1

## Bước 1. Nhận lệnh
Đọc lệnh JSON. Kiểm tra đủ: `task_id`, `action` (`search_group_posts`), `group_id`, `post_keyword`, `limit`, `fields_to_extract`. Thiếu trường bắt buộc thì trả `status: "error"`.

## Bước 2. Xác định nhóm
Mở trang feed của nhóm `https://www.facebook.com/groups/<group_id>/` bằng trình duyệt đã đăng nhập. Nếu lệnh có `group_ids` thì quét lần lượt từng nhóm, nghỉ `group_pause_seconds` (mặc định 20 giây) giữa 2 nhóm. Nếu bị chuyển về trang đăng nhập / checkpoint thì dừng, báo hết phiên, cần chạy `--login` lại. Nhóm nào lỗi thì bỏ qua, quét nhóm tiếp theo.

## Bước 3. Tìm bài
Đọc trang feed của nhóm (không dùng trang search của nhóm, vì trang đó trộn gợi ý người). Chỉ nhận bài có link thật dạng `/permalink/` hoặc `/posts/`. Lọc theo từ khóa (khớp cả có dấu lẫn không dấu). Cuộn trang tối đa 4 lần để đủ `limit` bài (tối đa 20).

## Bước 4. Lấy thông tin
Với mỗi bài, lấy đủ trường cơ bản + các trường trong `fields_to_extract`. Không có thì để `null`. Không bịa.
Sau đó áp bộ lọc của lệnh: `levels`, khoảng giá, `location_keyword`, `exclude_keywords`, `only_new` (bỏ bài đã thấy trong `seen.json`), và `customer_profile` nếu có.

## Bước 5. Trả kết quả
Trả 1 JSON duy nhất theo `task-schema.json`. Không gửi thêm file, không giải thích ngoài `summary`. Ở chế độ `--watch`, lặp lại từ bước 2 sau mỗi `watch_interval_minutes`, bài mới ghi thêm vào `watch-new.jsonl`.
