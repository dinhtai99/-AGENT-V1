@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================================
echo   🛑 DỪNG MÁY CHỦ VÀ TIẾN TRÌNH QUÉT NGẦM AGENT-V1 (WINDOWS)
echo ================================================================
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8080" ^| findstr "LISTENING"') do (
    taskkill /F /T /PID %%a >nul 2>&1
    echo ✅ Đã dừng máy chủ trên cổng 8080 (PID %%a).
)
wmic process where "commandline like '%%browser_worker.py%%' or commandline like '%%server.py%%'" call terminate >nul 2>&1
echo ✅ Đã dừng tất cả các tiến trình quét ngầm.
pause
