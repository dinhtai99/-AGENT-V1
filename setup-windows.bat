@echo off
chcp 65001 >nul
echo ============================================================
echo   🚀 AGENT-V1 — CÀI ĐẶT MÔI TRƯỜNG CHẠY TRÊN WINDOWS PC
echo ============================================================
cd /d "%~dp0"

echo [1/3] Kiểm tra Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ❌ [LỖI] Máy tính chưa cài Python hoặc chưa tích 'Add Python to PATH'!
    echo 👉 Hãy vào https://www.python.org/downloads/ tải bản Python 3.11 hoặc 3.12 mới nhất.
    echo ⚠️ LƯU Ý KHI CÀI: Nhớ tích vào ô [x] "Add Python to PATH" ở bước đầu tiên!
    echo.
    pause
    exit /b 1
)
python --version

echo.
echo [2/3] Cài đặt thư viện Playwright...
python -m pip install --upgrade pip
python -m pip install playwright

echo.
echo [3/3] Tải trình duyệt Chromium cho robot...
python -m playwright install chromium

echo.
echo ============================================================
echo   ✅ CÀI ĐẶT HOÀN TẤT THÀNH CÔNG!
echo   Bây giờ bạn chỉ cần nhấp đúp vào file 'start-web.bat'
echo   để mở giao diện điều khiển trên trình duyệt!
echo ============================================================
pause
