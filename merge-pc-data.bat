@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
echo =================================================================
echo   🤝 AGENT-V1 — HỢP NHẤT DỮ LIỆU KHÁCH HÀNG
echo =================================================================
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" merge_leads.py %*
) else (
  python merge_leads.py %*
)
pause
