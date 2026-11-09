# AGENT-V1 — Worker Facebook Groups

Worker nhận lệnh từ agent sếp (orchestrator), chạy đúng quy trình rồi trả kết quả về. Không tự hỏi lại, không tự làm thêm.

## Mô hình

```
Agent sếp (của bạn)
  -> chia việc, tổng hợp
  -> gửi 1 lệnh JSON xuống
AGENT-V1 (worker)
  -> vào nhóm Facebook A, hoặc tìm list nhóm
  -> tìm bài theo từ khóa
  -> lấy thông tin đã yêu cầu
  -> trả kết quả JSON về cho sếp
```

## Cách dùng

1. Giải nén file này vào `Desktop/AGENT-V1` trên Windows.
2. Mở `AGENT.md` — đây là prompt / luật làm việc của worker.
3. Mở `example-task.json` — sửa thành lệnh thật của bạn rồi gửi cho worker.
4. Worker chạy theo `WORKFLOW.md` và trả về đúng định dạng trong `task-schema.json`.

## File trong này

- `AGENT.md` — vai trò, luật, và lệnh Facebook worker được dùng.
- `WORKFLOW.md` — quy trình 5 bước, từng bước làm gì.
- `task-schema.json` — hợp đồng lệnh vào / kết quả ra.
- `example-task.json` — 1 lệnh mẫu: tìm bài trong 1 nhóm theo từ khóa.
- `example-task-find-groups.json` — 1 lệnh mẫu: tìm list nhóm theo từ khóa.
- `example-result.json` — kết quả mẫu worker phải trả về.

## Giới hạn thật

- Chỉ đọc được nhóm mà tài khoản Facebook đã kết nối được phép xem: nhóm public, hoặc nhóm bạn đã tham gia.
- Nhóm private ẩn mà bạn không phải thành viên thì không vào được.
- Mỗi lần tìm bài tối đa 20 bài, tìm nhóm tối đa 25 nhóm.
- Không cào hàng loạt, không liệt kê toàn bộ profile / bài / nhóm.

## Ghi chú khi bán / bàn giao

Người mua chỉ cần thay `example-task.json` bằng lệnh thật của họ: tên nhóm, từ khóa, trường cần lấy. Không cần sửa code của worker.
