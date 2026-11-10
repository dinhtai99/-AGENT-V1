# AGENT-V1 — Worker Facebook Groups

Worker nhận lệnh từ agent sếp (orchestrator), chạy đúng quy trình rồi trả kết quả về. Không tự hỏi lại, không tự làm thêm.

## Mô hình

```
Agent sếp (của bạn)
  -> chia việc, tổng hợp
  -> gửi 1 lệnh JSON xuống
AGENT-V1 (worker)
  -> vào nhóm Facebook A
  -> tìm bài theo từ khóa
  -> lọc đúng đối tượng khách hàng sếp mô tả
  -> trả kết quả JSON về cho sếp
```

## Cách chạy (Windows)

```
pip install -r requirements-browser.txt
playwright install chromium
python browser_worker.py --login
python browser_worker.py --task example-task-customer.json --out result.json --watch
```

Chi tiết đầy đủ trong `README-STANDALONE.md`.

## File trong này

- `browser_worker.py` — worker chính: đọc nhóm Facebook bằng trình duyệt.
- `filters.py` — trích xuất và lọc bài (không gọi Facebook).
- `AGENT.md` — vai trò, luật, và cách worker làm việc.
- `WORKFLOW.md` — quy trình 5 bước, từng bước làm gì.
- `task-schema.json` — hợp đồng lệnh vào / kết quả ra.
- `example-task.json` — 1 lệnh mẫu: tìm bài trong 1 nhóm theo từ khóa.
- `example-task-customer.json` — lệnh mẫu có `customer_profile`.
- `example-task-watch.json` / `example-task-watch-loose.json` — lệnh mẫu chạy watch.
- `example-result.json` — kết quả mẫu worker phải trả về.
- `task-test-browser-tuyen.json` — lệnh test nhanh trên nhóm cầu lông Hà Nội.

## Giới hạn thật

- Chỉ đọc được nhóm mà tài khoản Facebook đã đăng nhập được phép xem: nhóm public, hoặc nhóm bạn đã tham gia.
- Mỗi lần quét tối đa 20 bài, 1 nhóm 1 lúc. Không cào hàng loạt.
- Facebook đã xóa Groups API (22/4/2024) nên không có đường token nữa, chỉ còn đường trình duyệt.
- Phiên đăng nhập có thể hết hạn sau 1–2 tiếng quét, cần `--login` lại.
- Xem `README-STANDALONE.md` để biết toàn bộ giới hạn và rủi ro tài khoản.
