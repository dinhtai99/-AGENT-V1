@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 🚀 Đang tự động nén và đẩy bản mới nhất lên Netlify...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" deploy_netlify.py
) else (
  python deploy_netlify.py
)
echo.
pause
