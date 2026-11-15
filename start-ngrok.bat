@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -B share_ngrok.py
) else (
  python -B share_ngrok.py
)
pause
