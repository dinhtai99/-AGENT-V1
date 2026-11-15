# AGENT-V1 — Worker Săn Chủ Kinh Doanh Nhỏ Từ Các Nhóm Baito (Nhật Bản)

Worker nhận lệnh từ agent sếp (orchestrator) hoặc chạy độc lập, tự động vào **các nhóm Baito trên Facebook mà bạn đang tham gia**, lọc ra các **cá nhân kinh doanh nhỏ / chủ mô hình kinh doanh nhỏ (< 20 người)** tại Nhật Bản, sau đó thực hiện quy trình 2 bước:
1. **Lập danh sách xem người này đang kinh doanh gì** (thông tin cá nhân FB, SĐT Nhật/Việt, Line/Zalo, ngành nghề, tên quán/tiệm, ga tàu/khu vực, quy mô ước tính).
2. **Đi sâu tìm hiểu họ đang cần gì** (vị trí nhân sự đang thiếu, ca làm việc quá tải, mức lương chi trả, tệp khách hàng của quán, và nỗi đau vận hành như cần người gấp, mới khai trương mở rộng, hay cần nhân sự gắn bó lâu dài có hỗ trợ Visa).

## Mô hình hoạt động

```
Agent sếp (Orchestrator)
  -> gửi lệnh JSON (task-baito-nhom-da-tham-gia.json)
AGENT-V1 (Browser Worker)
  -> Bước 1: Tự lấy danh sách các nhóm Baito mà tài khoản FB đang tham gia (hoặc theo group_ids)
  -> Bước 2: Đọc bài mới trên từng nhóm, loại bỏ người đi xin việc & công ty môi giới/haken lớn
  -> Bước 3: Giữ lại chủ mô hình kinh doanh nhỏ (< 20 người): quán ăn Việt, izakaya, tiệm nail/spa, tạp hóa, xưởng bento, shop...
  -> Bước 4: Bóc tách Thông tin cá nhân + Mô hình kinh doanh + Phân tích sâu nhu cầu
  -> Bước 5: Xuất result.json + Danh sách tổng hợp chủ quán (leads.json & leads.csv mở bằng Excel)
```

## Cách chạy

### Điều khiển trên website

Sau khi cài đặt Playwright, mở `start-web.command` trên macOS hoặc `start-web.bat` trên Windows. File mở nhanh sẽ mở link truy cập riêng tại `http://127.0.0.1:8080`. Trên trang, bạn có thể đăng nhập Facebook từ chính website (kể cả trên máy tính khác), bắt đầu/dừng lượt quét, bật/tắt quét tự động, xem nhật ký và sửa cấu hình. Trạng thái và ghi chú chăm sóc được lưu vào `leads.json` và `leads.csv`.

Trong **Cấu hình nhóm, khách hàng mục tiêu và lịch quét**, sửa **Mô tả khách hàng mục tiêu** để chọn loại bài cần giữ. Worker dùng mô tả này khi quét; dashboard áp lại cùng tiêu chí cho bài đã có ngay sau khi lưu. Hệ thống hiện nhận diện ngành nghề, quy mô, khu vực, trình độ, mức giá/lương và ca làm từ mô tả. Trang cho biết những tiêu chí đã nhận diện; nội dung chưa nhận diện sẽ không được dùng để lọc. **Từ khóa nhóm** chỉ chọn nhóm để quét.

Trong hồ sơ từng khách hàng, mục **Mô tả khách hàng** có thể sửa trực tiếp để ghi thông tin riêng về khách đó. Nội dung sửa sẽ hiện trên thẻ, bảng và file CSV; nó không làm thay đổi bộ lọc. Khi đổi mô tả mục tiêu, khách không còn khớp sẽ được ẩn khỏi danh sách hiện tại, nhưng hồ sơ và CRM đã lưu vẫn còn trong `leads.json` để có thể hiện lại sau này.

Website tạo khóa truy cập trong `.web-access-key` (file này không đưa lên Git). Mọi trang và API đều yêu cầu link có khóa; sau khi mở link, trình duyệt ghi nhớ quyền truy cập. Trên trang có nút sao chép link để gửi cho người thứ hai và nút đổi link để thu hồi link cũ. Chỉ gửi link cho người được phép xem dữ liệu khách hàng và điều khiển quét.

Khi bấm **Đăng nhập Facebook**, một cửa sổ trong dashboard hiển thị trình duyệt Facebook đang chạy trên máy chủ. Bấm vào ô trên ảnh, nhập email/mật khẩu hoặc mã xác minh ở thanh bên dưới rồi bấm **Gõ**; dùng nút **Enter** để gửi. Phiên Facebook được lưu tự động vào `fb-state.json` sau khi đăng nhập thành công. Nội dung nhập không được ghi vào nhật ký hoặc file của dashboard.

Để chia sẻ qua Internet, dùng cách bật máy chủ tạm thời bên dưới. Khi mở trang bằng địa chỉ HTTPS, nút **Sao chép link truy cập** cũng có thể tạo lại link có khóa để gửi cho người thứ hai. Máy chủ và đường kết nối phải tiếp tục chạy trong lúc sử dụng.

### Dùng máy Mac của bạn làm máy chủ tạm thời

Mở `start-share.command` khi muốn cho người ở máy khác dùng. File này khởi động website trên `127.0.0.1:8080`, tạo link HTTPS tạm thời qua Cloudflare Quick Tunnel, mở website trên máy bạn và sao chép link có khóa truy cập vào bộ nhớ tạm. Gửi **toàn bộ link** đó cho người thứ hai; họ mở link để xem dữ liệu, đăng nhập Facebook và quét trên cùng máy chủ. Không cần tạo tài khoản website. Giữ cửa sổ Terminal mở; bấm **Ctrl+C** để tắt đường kết nối và website. Mỗi lần bật sẽ có link mới và phải gửi lại. Máy cần luôn bật và có Internet trong lúc dùng.

Máy cần cài `cloudflared` trước khi dùng file này. Cloudflare [ghi rõ](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) Quick Tunnel dành cho thử nghiệm, không bảo đảm thời gian hoạt động; nếu cần địa chỉ cố định và dùng lâu dài, hãy chuyển sang Named Tunnel. Link chứa khóa truy cập, vì vậy chỉ gửi riêng cho người được phép xem dữ liệu và điều khiển quét. `start-web.command` vẫn chỉ dùng trên máy của bạn, không tạo link Internet.

Máy chủ web cần tiếp tục chạy trong lúc quét. Nếu không dùng file khởi động, chạy `python3 server.py --open` một lần để mở trang. Chỉ mở `dashboard.html` trực tiếp từ ổ đĩa thì các nút điều khiển sẽ không hoạt động.

Chỉ cần cài Playwright một lần trên máy mới:

```bash
pip install -r requirements-browser.txt
playwright install chromium
```

Các lệnh terminal tương đương, nếu cần chạy thủ công:

```bash
# Đăng nhập Facebook 1 lần (lưu phiên vào fb-state.json)
python browser_worker.py --login

# Xem danh sách các nhóm Baito đã tham gia
python browser_worker.py --list-groups

# Quét 1 lượt
python browser_worker.py --task task-baito-nhom-da-tham-gia.json --out result.json

# Mở bản Dashboard offline
python dashboard.py --open

# Quét tự động qua terminal
python browser_worker.py --task task-baito-nhom-da-tham-gia.json --out result.json --watch
```

## Kết quả đầu ra

Ngoài `result.json` trả về cho Agent sếp, worker tự động tổng hợp và cộng dồn danh sách chủ kinh doanh (gom nhóm không trùng lặp theo từng chủ quán) vào:
- `dashboard.html` — Bản xem offline: hồ sơ chủ quán, tìm kiếm, bộ lọc, bài gốc và CRM lưu trong trình duyệt. Để điều khiển quét và lưu CRM xuống file, mở trang qua `server.py`.
- `leads.json` — Hồ sơ chi tiết từng chủ quán (`personal_info`, `step1_business_model`, `step2_deep_needs`, `source_posts`).
- `leads.csv` — Bảng tính mở trực tiếp bằng Excel / Google Sheets (UTF-8-SIG), liệt kê rõ từng chủ quán đang kinh doanh gì và đang cần gì.

## Các file chính trong dự án

- `server.py` / `start-web.command` / `start-web.bat` — Máy chủ web cục bộ và file mở nhanh.
- `share_server.py` / `start-share.command` — Bật/tắt máy chủ cục bộ cùng link HTTPS tạm thời để chia sẻ.
- `dashboard.py` / `dashboard_template.html` — Tạo giao diện và bản xem offline (`dashboard.html`).
- `browser_worker.py` — Worker chính: tự tìm nhóm Baito đã tham gia, đọc bài, trích thông tin cá nhân người đăng (`author_name`, `author_url`, `group_member_url`) và xuất danh sách leads.
- `filters.py` — Bộ phân loại chủ kinh doanh nhỏ (< 20 người), nhận diện mô hình kinh doanh tại Nhật và phân tích sâu nhu cầu/nỗi đau vận hành.
- `watchdog.py` / `start-watch.bat` — Giám sát chạy 24/24, tự khởi động lại nếu worker bị treo/crash.
- `task-baito-nhom-da-tham-gia.json` — Lệnh mặc định: tự quét các nhóm Baito bạn đang tham gia trên FB.
- `task-schema.json` — Hợp đồng dữ liệu đầu vào / đầu ra.
- `AGENT.md` / `WORKFLOW.md` — Quy tắc và quy trình 5 bước của worker.
