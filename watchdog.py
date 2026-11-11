#!/usr/bin/env python3
"""
AGENT-V1 — Watchdog: keep the watch worker running 24/24.

The worker (browser_worker.py --watch) can die without anyone noticing:
a crash, a reboot, or the PC waking from sleep. The watchdog checks
every few minutes and restarts it — but only when the worker died
unexpectedly. It never restarts a worker that stopped on purpose:

- watch-status.json "stopped" + stop_reason "session_expired":
  the Facebook session is dead; restarting would fail identically.
  A human must run `python browser_worker.py --login` first.
- watch-status.json "stopped" + any other reason: the worker decided
  something structural is wrong; a human should look first.

Restart happens when the worker process is gone (watch.pid dead) or
its heartbeat is stale (no scan for over 2 intervals + 15 minutes)
while the status is not "stopped". A stale heartbeat needs two
strikes in a row before the worker is killed — a PC waking from
sleep looks stale for one check, then scans and recovers.

Usage:
    python watchdog.py --task example-task-multi.json
    python watchdog.py --task example-task-multi.json --once
    python watchdog.py --task example-task-multi.json --check-every-minutes 5

Run it from start-watch.bat, or add it to Windows Task Scheduler
(trigger: at logon) so it survives reboots. Only one watchdog may
run at a time (guarded by watchdog.pid). Stdlib only, no Playwright.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _log(root: Path, msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with (root / "watchdog.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    """True when a process with this PID is running (Windows + POSIX)."""
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == str(pid):
                    return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _read_status(root: Path) -> dict:
    try:
        data = json.loads((root / "watch-status.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _status_age_minutes(status: dict):
    """Minutes since the last scan heartbeat, or None if unknown."""
    try:
        updated = time.strptime(status.get("updated_at", ""), "%Y-%m-%dT%H:%M:%S")
        return (time.time() - time.mktime(updated)) / 60.0
    except Exception:
        return None


def _decide(status: dict, pid_alive: bool, age_minutes,
            stale_after_minutes: float) -> tuple[str, str]:
    """Pure decision: (action, reason). action: skip | start | restart.

    - "stopped" status means the worker quit on purpose: never restart.
    - A fresh heartbeat proves the worker is alive even if the pid
      file is missing, so a missing pid alone never triggers a restart.
    - A stale heartbeat with a dead process means "start"; with a live
      process it means "restart" (looks hung).
    """
    if (status or {}).get("status") == "stopped":
        return "skip", (
            f"worker stopped ({status.get('stop_reason', '?')}); "
            "needs a human, not a restart")
    if age_minutes is not None and age_minutes <= stale_after_minutes:
        return "skip", "heartbeat fresh — worker is alive"
    if pid_alive:
        if age_minutes is None:
            return "skip", "worker process alive, heartbeat file missing"
        return "restart", "heartbeat stale, process alive — looks hung"
    return "start", "worker not running"


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception:
        pass


def _start_worker(root: Path, task_file: str, out_file: str) -> None:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_CONSOLE
    subprocess.Popen(
        [sys.executable, "browser_worker.py", "--task", task_file,
         "--out", out_file, "--watch"],
        cwd=str(root), creationflags=flags)


def _load_state(root: Path) -> dict:
    try:
        data = json.loads((root / "watchdog-state.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(root: Path, state: dict) -> None:
    try:
        (root / "watchdog-state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def check_once(root: Path, task_file: str, out_file: str,
               stale_after_minutes: float) -> str:
    """One check: decide and act. Returns the action taken."""
    status = _read_status(root)
    pid = 0
    try:
        pid = int((root / "watch.pid").read_text(encoding="utf-8").strip())
    except Exception:
        pid = 0
    age = _status_age_minutes(status)
    action, reason = _decide(status, _pid_alive(pid), age, stale_after_minutes)
    state = _load_state(root)
    if action == "restart":
        # Two stale strikes before killing: a PC waking from sleep
        # looks stale once, then scans and recovers on its own.
        state["stale_strikes"] = state.get("stale_strikes", 0) + 1
        if state["stale_strikes"] < 2:
            _log(root, f"Heartbeat stale ({reason}); "
                       "waiting one more check before restarting.")
            _save_state(root, state)
            return "wait"
        state["stale_strikes"] = 0
        _save_state(root, state)
        _kill_pid(pid)
        _start_worker(root, task_file, out_file)
        _log(root, f"Worker hung ({reason}); killed PID {pid} and restarted it.")
        return "restarted"
    state["stale_strikes"] = 0
    _save_state(root, state)
    if action == "start":
        _start_worker(root, task_file, out_file)
        _log(root, f"Starting worker ({reason}).")
        return "started"
    _log(root, f"OK: {reason}.")
    return "skip"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AGENT-V1 watchdog")
    parser.add_argument("--task", default="example-task-multi.json")
    parser.add_argument("--out", default="result.json")
    parser.add_argument("--root", default=None,
                        help="Folder holding the worker and status files "
                             "(default: this script's folder)")
    parser.add_argument("--check-every-minutes", type=int, default=5)
    parser.add_argument("--once", action="store_true",
                        help="Check once, then exit (for Task Scheduler)")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else Path(__file__).parent
    # Only one watchdog at a time.
    guard = root / "watchdog.pid"
    try:
        old = int(guard.read_text(encoding="utf-8").strip())
    except Exception:
        old = 0
    if old and _pid_alive(old):
        print(f"Another watchdog is already running (PID {old}). Exiting.")
        return 0
    try:
        guard.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    # Stale = no scan for 2 intervals + 15 minutes of grace.
    interval = 30
    try:
        task = json.loads(Path(args.task).read_text(encoding="utf-8"))
        interval = max(5, int(task.get("watch_interval_minutes", 30) or 30))
    except Exception:
        pass
    stale_after = 2 * interval + 15
    _log(root, f"Watchdog started (task {args.task}, check every "
               f"{args.check_every_minutes} min, stale after {stale_after} min).")
    try:
        while True:
            check_once(root, args.task, args.out, stale_after)
            if args.once:
                break
            time.sleep(max(1, args.check_every_minutes) * 60)
    except KeyboardInterrupt:
        print("\nWatchdog stopped.")
    finally:
        try:
            guard.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
