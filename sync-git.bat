@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo =================================================================
echo   🔄 AGENT-V1 — ĐỒNG BỘ & HỢP NHẤT DỮ LIỆU TỰ ĐỘNG QUA GITHUB
echo =================================================================

REM 1. Kích hoạt Git Merge Driver thông minh cho leads.json
git config merge.json-leads.name "JSON Leads 3-Way Merger"
if exist ".venv\Scripts\python.exe" (
  git config merge.json-leads.driver ".venv\Scripts\python.exe merge_leads.py --git-merge %%O %%A %%B"
) else (
  git config merge.json-leads.driver "python merge_leads.py --git-merge %%O %%A %%B"
)

REM 2. Lưu lại các bài đăng và khách hàng đã quét mới nhất trên PC
echo 📦 Đang lưu dữ liệu mới trên máy này...
git add leads.json leads.csv task-baito-nhom-da-tham-gia.json result.json dashboard.html dist/ 2>nul
git commit -m "Auto-sync scan data from PC [%date% %time%]" 2>nul

REM 3. Kéo dữ liệu từ Mac về và tự động gộp
echo ⬇️ Đang kéo dữ liệu từ máy khác về và tự động gộp...
git pull --no-rebase origin main

REM 4. Làm mới Dashboard và file Excel sau khi gộp
echo 📊 Đang làm mới Dashboard HTML và file Excel sau khi gộp...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" dashboard.py --no-save
) else (
  python dashboard.py --no-save
)

REM 5. Đẩy bản dữ liệu đã gộp lên GitHub để máy kia cùng nhận
echo ⬆️ Đang đẩy dữ liệu hợp nhất lên GitHub...
git add leads.json leads.csv dashboard.html dist\index.html 2>nul
git commit -m "Merged leads data [%date% %time%]" 2>nul
git push origin main

echo.
echo =================================================================
echo   🎉 ĐỒNG BỘ HOÀN TẤT THÀNH CÔNG!
echo   Dữ liệu cả 2 máy (Mac & PC) đã được gộp chung trọn vẹn 100%!
echo =================================================================
pause
