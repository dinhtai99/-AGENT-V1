# WORKFLOW — Quy trình 5 bước của AGENT-V1

## Bước 1. Nhận lệnh
Đọc lệnh JSON. Kiểm tra đủ: `task_id`, `action`, và tùy action mà cần `group_id` / `group_name` hoặc `group_keywords`, `post_keyword`, `limit`, `fields_to_extract`.

## Bước 2. Xác định nhóm
- Nếu có `group_id` hoặc `group_name`: chạy `groups details` để xác nhận nhóm tồn tại và xem được.
- Nếu chỉ có `group_keywords`: chạy `groups search --keywords` để tìm list nhóm, chọn đúng nhóm theo tên / mô tả, ghi lại các nhóm khác tìm thấy (nếu có).
- Nếu không tìm thấy / không xem được: dừng, trả `status: "error"`.

## Bước 3. Tìm bài
- Chạy `groups posts --group-id <id> --query '<post_keyword>' --limit <limit>`.
- Nếu có `date_from` / `date_to`: lọc bài theo `created_at` trong khoảng đó.
- Nếu cần bình luận: với mỗi bài được chọn, chạy `post comments read` khi lệnh yêu cầu.

## Bước 4. Lấy thông tin
Với mỗi bài, lấy đủ trường cơ bản + các trường trong `fields_to_extract`.
Không có thì để `null`. Không bịa.

## Bước 5. Trả kết quả
Trả 1 JSON duy nhất theo `task-schema.json`. Không gửi thêm file, không giải thích ngoài `summary`.
