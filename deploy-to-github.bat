@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 🚀 Đang gộp dữ liệu (Mac ^<^> Windows) và đẩy lên GitHub Pages...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" deploy_github.py
) else (
  python deploy_github.py
)
echo.
pause
