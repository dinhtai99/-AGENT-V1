@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   🔐 ĐĂNG NHẬP FACEBOOK ĐỂ LƯU PHIÊN (fb-state.json)
echo ============================================================
python browser_worker.py --login
pause
