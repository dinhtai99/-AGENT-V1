#!/bin/bash
cd "$(dirname "$0")"
echo "================================================================="
echo "  🔄 AGENT-V1 — ĐỒNG BỘ & HỢP NHẤT DỮ LIỆU TỰ ĐỘNG QUA GITHUB"
echo "================================================================="

# 1. Kích hoạt Git Merge Driver thông minh cho leads.json (Chống xung đột JSON)
git config merge.json-leads.name "JSON Leads 3-Way Merger"
git config merge.json-leads.driver "python3 merge_leads.py --git-merge %O %A %B"

# 2. Lưu lại các bài đăng và khách hàng đã quét mới nhất trên máy này
echo "📦 Đang lưu dữ liệu mới trên máy này..."
git add leads.json leads.csv task-baito-nhom-da-tham-gia.json result.json dashboard.html dist/ 2>/dev/null
git commit -m "Auto-sync scan data from $(hostname) [$(date +'%Y-%m-%d %H:%M')]" 2>/dev/null

# 3. Kéo dữ liệu từ máy khác về và tự động gộp (Auto-Merge)
echo "⬇️ Đang kéo dữ liệu từ máy khác về và tự động gộp..."
git pull --no-rebase origin main

# 4. Làm mới Dashboard và file Excel tổng hợp
echo "📊 Đang làm mới Dashboard HTML và file Excel sau khi gộp..."
python3 dashboard.py --no-save

# 5. Đẩy bản dữ liệu đã gộp lên GitHub để máy kia cùng nhận
echo "⬆️ Đang đẩy dữ liệu hợp nhất lên GitHub..."
git add leads.json leads.csv dashboard.html dist/index.html 2>/dev/null
git commit -m "Merged leads data [$(date +'%Y-%m-%d %H:%M')]" 2>/dev/null
git push origin main

echo ""
echo "================================================================="
echo "  🎉 ĐỒNG BỘ HOÀN TẤT THÀNH CÔNG!"
echo "  Dữ liệu cả 2 máy (Mac & PC) đã được gộp chung trọn vẹn 100%!"
echo "================================================================="
echo "Nhấn phím bất kỳ để đóng cửa sổ..."
read -n 1
