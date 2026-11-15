@echo off
REM AGENT-V1 — start the 24/24 Baito Japan Lead Finder watch (worker + watchdog).
REM The watchdog starts the worker if needed and restarts it if it dies.
cd /d %~dp0
start "AGENT-V1 watchdog" python watchdog.py --task task-baito-nhom-da-tham-gia.json
echo Watchdog started in its own window. It will start the worker (task-baito-nhom-da-tham-gia.json).
echo Results are saved to result.json, leads.json, and leads.csv.
echo Keep both windows open. Close this window any time; the watchdog keeps running.
pause
