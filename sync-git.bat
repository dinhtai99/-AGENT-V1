@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================================
echo   🔄 ĐỒNG BỘ CODE ^& DỮ LIỆU KHÁCH HÀNG (MAC ^<^> WINDOWS)
echo ================================================================
echo [1/3] Kéo code mới nhất từ nhánh main...
git pull --rebase origin main
echo.
echo [2/3] Gộp dữ liệu khách hàng (leads.json, CRM, seen.json) và cập nhật GitHub Pages...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" deploy_github.py
) else (
  python deploy_github.py
)
echo.
echo [3/3] Hoàn tất đồng bộ!
pause
