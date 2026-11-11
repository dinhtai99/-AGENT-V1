@echo off
REM AGENT-V1 — start the 24/24 watch (worker + watchdog).
REM The watchdog starts the worker if needed and restarts it if it dies.
REM To stop everything: press Ctrl+C in the watchdog window AND in the
REM worker window (or close both windows).
cd /d %~dp0
start "AGENT-V1 watchdog" python watchdog.py --task example-task-multi.json
echo Watchdog started in its own window. It will start the worker (example-task-multi.json).
echo Keep both windows open. Close this window any time; the watchdog keeps running.
pause
