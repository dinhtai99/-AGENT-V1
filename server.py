#!/usr/bin/env python3
"""AGENT-V1 — Interactive Web Control Center & Dashboard Server.

Runs a local web server (default http://127.0.0.1:8080) that turns AGENT-V1 into
a complete, interactive web application:
1. Trigger real scans directly on the web interface ("Quét ngay").
2. Toggle 24/7 automated watch mode with live countdown ("Chế độ tự động").
3. View real-time terminal logs directly inside the web browser.
4. Manage Facebook login sessions directly from the web.
5. Edit target Baito groups, keywords, and search settings without touching JSON.
6. 2-way CRM synchronization: changing status/notes on the web saves immediately
   to leads.json and leads.csv on disk!

Usage:
    python3 server.py                # starts web app on http://127.0.0.1:8080
    python3 server.py --port 9000    # use custom port
    python3 server.py --open         # starts and opens default browser
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import dashboard  # noqa: E402
from filters import load_json, parse_customer_profile, save_json, save_leads_csv  # noqa: E402
from remote_login import RemoteLoginSession  # noqa: E402
from web_access import COOKIE_NAME, QUERY_NAME, WebAccess  # noqa: E402

DEFAULT_PORT = 8080
DEFAULT_HOST = "127.0.0.1"
CRM_STATUSES = {"new", "contacted", "hot", "won", "skip"}
TASK_FILE = "task-baito-nhom-da-tham-gia.json"
# Settings the web form may change, with their expected type
EDITABLE_TASK_FIELDS = {
    "group_ids": list,
    "limit": int,
    "watch_interval_minutes": int,
    "discover_joined_groups": bool,
    "post_keyword": str,
}


def find_python_executable() -> str:
    """Find the best Python binary to run worker tasks.

    Prefers virtual environment (~/.venv-agent) if available, else current interpreter.
    """
    home = Path.home()
    candidates = [
        home / ".venv-agent" / "Scripts" / "python.exe",
        home / ".venv-agent" / "bin" / "python",
        home / ".venv-agent" / "bin" / "python3",
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
        ROOT / "venv" / "Scripts" / "python.exe",
        ROOT / "venv" / "bin" / "python",
        Path(sys.executable),
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return sys.executable or "python3"


class WorkerManager:
    """Manages worker execution, live logs, and automated watch scheduling."""

    def __init__(self, root: Path):
        self.root = root
        self.python_bin = find_python_executable()
        # Re-entrant: add_log() and _read_json() are called while the lock is held
        self.lock = threading.RLock()
        self.crm_lock = threading.Lock()
        self.is_scanning = False
        self.scan_cancel_requested = False
        self.session_expired = False
        self.remote_login = RemoteLoginSession(root / "fb-state.json")
        self.watch_mode = False
        self.watch_generation = 0
        self.watch_thread: threading.Thread | None = None
        self.current_process: subprocess.Popen | None = None
        self.scan_thread: threading.Thread | None = None
        self.last_scan_time: float | None = None
        self.next_scan_time: float | None = None
        self.last_summary = ""
        self.log_buffer: deque[dict] = deque(maxlen=2000)
        self.log_counter = 0
        self.last_netlify_sync_ok = True

        # Check existing result/leads for initial summary
        res = self._read_json("result.json")
        if res and isinstance(res, dict):
            self.last_summary = res.get("summary", "")

    def _read_json(self, name: str):
        p = self.root / name
        if not p.exists():
            return None
        try:
            return load_json(p)
        except Exception:
            return None

    def add_log(self, text: str, level: str = "info") -> None:
        if any(k in text for k in ("BỊ ĐĂNG XUẤT", "LOGOUT", "checkpoint", "phiên đăng nhập đã hết hạn")):
            level = "error"
            self.session_expired = True

        with self.lock:
            self.log_counter += 1
            entry = {
                "id": self.log_counter,
                "time": time.strftime("%H:%M:%S"),
                "text": text,
                "level": level,
            }
            self.log_buffer.append(entry)
        # Also print to server terminal stdout
        print(f"[{entry['time']}] {text}")

    def get_logs(self, since_id: int = 0) -> list[dict]:
        with self.lock:
            if since_id <= 0:
                return list(self.log_buffer)
            return [l for l in self.log_buffer if l["id"] > since_id]

    def has_session(self) -> bool:
        p = self.root / "fb-state.json"
        if not p.exists() or p.stat().st_size < 50:
            return False
        try:
            data = self._read_json("fb-state.json")
            if isinstance(data, dict):
                cookies = data.get("cookies", [])
                has_c_user = any(c.get("name") == "c_user" and c.get("value") for c in cookies)
                return has_c_user
        except Exception:
            pass
        return False

    def get_status(self) -> dict:
        with self.lock:
            next_in = None
            if self.watch_mode and self.next_scan_time:
                next_in = max(0, int(self.next_scan_time - time.time()))

            leads_doc = self._read_json("leads.json")
            leads_count = 0
            if isinstance(leads_doc, dict):
                leads_count = leads_doc.get("total_leads", 0) or len(leads_doc.get("leads", []))
            elif isinstance(leads_doc, list):
                leads_count = len(leads_doc)

            res = self._read_json("result.json")
            groups_count = len((res or {}).get("groups", []))

            valid_session = self.has_session() and not self.session_expired

            return {
                "server_active": True,
                "is_scanning": self.is_scanning,
                "is_logging_in": self.remote_login.is_active(),
                "watch_mode": self.watch_mode,
                "next_scan_seconds": next_in,
                "last_scan_time": (
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_scan_time))
                    if self.last_scan_time
                    else None
                ),
                "last_summary": self.last_summary,
                "has_login_session": valid_session,
                "session_expired": not valid_session,
                "leads_count": leads_count,
                "groups_count": groups_count,
                "latest_log_id": self.log_counter,
            }

    def start_scan(self, demo: bool = False, deep: bool = False, task_file: str = "task-baito-nhom-da-tham-gia.json") -> tuple[bool, str]:
        with self.lock:
            if self.is_scanning or self.remote_login.is_active():
                return False, "Worker đang quét hoặc đăng nhập. Vui lòng đợi hoàn tất."
            if not demo and not self.has_session():
                self.session_expired = True
                self.add_log("🚨 CẢNH BÁO: Phiên Facebook đã hết hạn hoặc bị đăng xuất! Vui lòng bấm 'Đăng nhập FB' trước khi quét.", "error")
                return False, "🚨 Phiên Facebook đã hết hạn hoặc bị đăng xuất! Vui lòng bấm 'Đăng nhập FB' trước khi quét."
            self.is_scanning = True
            self.scan_cancel_requested = False

        def run_thread():
            if not demo and (self.root / ".git").exists():
                try:
                    import importlib
                    import deploy_github
                    importlib.reload(deploy_github)
                    self.add_log("🔄 Đang đồng bộ dữ liệu (leads & seen) từ GitHub trước khi quét...", "info")
                    with self.crm_lock:
                        ok_sync, msg_sync = deploy_github.sync_from_github(self.root, fetch=True)
                    if ok_sync:
                        self.add_log(msg_sync, "info")
                except Exception as sync_err:
                    self.add_log(f"Bỏ qua bước kéo dữ liệu trước khi quét: {sync_err}", "warn")
            if deep:
                self.add_log(f"Bắt đầu QUÉT SÂU 1 NĂM (Tìm kiếm từ khóa ngành nghề trên toàn bộ các nhóm)...", "info")
            else:
                self.add_log(f"Bắt đầu quét Baito (Worker Python: {self.python_bin})", "info")
            cmd = [
                self.python_bin,
                "-u",
                str(self.root / "browser_worker.py"),
                "--task",
                task_file,
                "--out",
                "result.json",
                "--debug",
            ]
            if demo:
                cmd.append("--demo")
            if deep:
                cmd.append("--deep")

            try:
                with self.lock:
                    cancel_before_start = self.scan_cancel_requested
                if cancel_before_start:
                    self.add_log("Đã hủy lượt quét trước khi bắt đầu.", "warn")
                    return
                child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
                with self.lock:
                    self.current_process = proc
                    cancel = self.scan_cancel_requested
                if cancel:
                    proc.terminate()

                for line in iter(proc.stdout.readline, ""):
                    s = line.rstrip()
                    if s:
                        lvl = "warn" if ("error" in s.lower() or "fail" in s.lower() or "checkpoint" in s.lower()) else "info"
                        self.add_log(s, lvl)

                proc.wait()
                exit_code = proc.returncode

                if exit_code == 0:
                    self.add_log("Quét hoàn tất thành công. Đang cập nhật Dashboard...", "success")
                else:
                    self.add_log(f"Quét kết thúc với mã lỗi exit_code={exit_code}", "error")

            except Exception as exc:
                self.add_log(f"Lỗi khi chạy worker: {exc}", "error")

            finally:
                with self.lock:
                    self.current_process = None
                    self.is_scanning = False
                    self.scan_cancel_requested = False
                    self.last_scan_time = time.time()
                    res = self._read_json("result.json")
                    if res and isinstance(res, dict):
                        self.last_summary = res.get("summary", "")

                try:
                    # Regenerate dashboard.html and sync leads
                    with self.crm_lock:
                        import importlib
                        import dashboard
                        importlib.reload(dashboard)
                        dashboard.generate(self.root, save_leads=True)
                    self.add_log("Dashboard và file leads.json / leads.csv đã được làm mới.", "info")

                    # Archive scan history by date and time
                    try:
                        today_str = time.strftime("%Y-%m-%d")
                        time_str = time.strftime("%H%M%S")
                        hist_dir = self.root / "history" / today_str
                        hist_dir.mkdir(parents=True, exist_ok=True)
                        res_path = self.root / "result.json"
                        if res_path.exists():
                            snap_path = hist_dir / f"scan_{time_str}.json"
                            shutil.copyfile(res_path, snap_path)
                            self.add_log(f"Đã lưu bản sao lưu lịch sử đợt quét: history/{today_str}/{snap_path.name}", "info")
                    except Exception as hist_err:
                        self.add_log(f"Lỗi khi lưu lịch sử quét: {hist_err}", "warn")

                    # Auto-deploy to GitHub Pages / Netlify on scan completion if configured
                    if (self.root / ".git").exists():
                        try:
                            import deploy_github
                            importlib.reload(deploy_github)
                            self.add_log("Đang tự động đồng bộ dữ liệu mới lên GitHub Pages...", "info")
                            ok, msg = deploy_github.deploy()
                            if ok:
                                self.add_log(f"Đã cập nhật GitHub Pages thành công! ({msg})", "success")
                            else:
                                self.add_log(f"Chưa thể cập nhật GitHub Pages: {msg}", "warn")
                        except Exception as gh_err:
                            self.add_log(f"Lỗi đồng bộ GitHub Pages: {gh_err}", "warn")
                    elif (self.root / ".netlify-config.json").exists():
                        try:
                            import deploy_netlify
                            importlib.reload(deploy_netlify)
                            if deploy_netlify.load_config().get("auth_token"):
                                self.add_log("Đang tự động đồng bộ dữ liệu mới lên Netlify...", "info")
                                ok, msg = deploy_netlify.deploy()
                                if ok:
                                    self.add_log(f"Đã cập nhật Netlify thành công! ({msg})", "success")
                                else:
                                    self.add_log(f"Chưa thể cập nhật Netlify: {msg}", "warn")
                        except Exception as nf_err:
                            self.add_log(f"Lỗi đồng bộ Netlify: {nf_err}", "warn")
                except Exception as exc:
                    self.add_log(f"Lỗi khi làm mới dashboard: {exc}", "error")

        th = threading.Thread(target=run_thread, daemon=True)
        self.scan_thread = th
        th.start()
        return True, "Đã khởi động tiến trình quét trong nền."

    def stop_scan(self) -> tuple[bool, str]:
        with self.lock:
            if not self.is_scanning:
                return False, "Không có lượt quét đang chạy."
            self.scan_cancel_requested = True
            proc = self.current_process
        self.add_log("🛑 Nhận được yêu cầu DỪNG QUÉT từ người dùng! Đang tắt worker...", "warn")
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=10,
                    )
                elif hasattr(os, "killpg") and hasattr(os, "getpgid"):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except Exception:
                        proc.terminate()
                else:
                    proc.terminate()
            except Exception as e:
                self.add_log(f"Lỗi khi dừng tiến trình: {e}", "warn")
                try:
                    proc.kill()
                except Exception:
                    pass
        return True, "Đã gửi lệnh dừng lượt quét thành công."

    def start_group_search(self, query: str = "kinh doanh tại nhật", min_members: int = 5000, limit: int = 10) -> tuple[bool, str]:
        with self.lock:
            if self.is_scanning or self.remote_login.is_active():
                return False, "Worker đang quét hoặc tìm kiếm. Vui lòng đợi hoàn tất."
            self.is_scanning = True
            self.scan_cancel_requested = False

        def run_search_thread():
            self.add_log(f"🔍 Bắt đầu TÌM KIẾM NHÓM FACEBOOK: Từ khóa '{query}' (Thành viên >= {min_members:,}, Tối đa {limit} nhóm)...", "info")
            cmd = [
                self.python_bin,
                "-u",
                str(self.root / "browser_worker.py"),
                "--search-groups",
                query,
                "--min-members",
                str(min_members),
                "--limit-groups",
                str(limit),
                "--debug",
            ]
            try:
                child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(self.root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=child_env,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
                with self.lock:
                    self.current_process = proc

                if proc.stdout:
                    for line in iter(proc.stdout.readline, ""):
                        stripped = line.rstrip()
                        if stripped:
                            self.add_log(stripped, "info")
                    proc.stdout.close()

                exit_code = proc.wait()
                if exit_code == 0:
                    self.add_log(f"Tìm nhóm hoàn tất! Đã lưu danh sách nhóm vào discovered_groups.json", "success")
                else:
                    self.add_log(f"Tìm nhóm kết thúc với mã lỗi exit_code={exit_code}", "warn")
            except Exception as exc:
                self.add_log(f"Lỗi khi tìm nhóm: {exc}", "error")
            finally:
                with self.lock:
                    self.current_process = None
                    self.is_scanning = False
                    self.scan_cancel_requested = False
                try:
                    with self.crm_lock:
                        import dashboard
                        dashboard.generate(self.root, save_leads=False)
                    self.add_log("Dashboard đã cập nhật danh sách nhóm mới tìm thấy.", "info")
                except Exception as exc:
                    self.add_log(f"Lỗi khi cập nhật dashboard: {exc}", "warn")

        th = threading.Thread(target=run_search_thread, daemon=True)
        self.scan_thread = th
        th.start()
        return True, "Đã khởi động tiến trình tìm kiếm nhóm Facebook."

    def add_groups_and_scan(self, group_ids: list[str], deep: bool = True) -> tuple[bool, str]:
        # Master task file persistence
        task_path = self.root / "task-baito-nhom-da-tham-gia.json"
        task = self._read_json(task_path.name) or {}
        existing_groups = [str(g).strip() for g in task.get("group_ids") or []]
        for gid in group_ids:
            gid = str(gid).strip()
            if gid and gid not in existing_groups:
                existing_groups.append(gid)
        task["group_ids"] = existing_groups
        with open(task_path, "w", encoding="utf-8") as f:
            json.dump(task, f, ensure_ascii=False, indent=2)

        # Check which groups have ALREADY been scanned in result.json
        res = self._read_json("result.json") or {}
        scanned_gids = {
            str(g["id"]): g
            for g in res.get("groups", [])
            if g.get("id") and g.get("raw_posts_read", 0) > 0 and not g.get("error")
        }

        # Filter strictly: only scan the new requested groups that haven't been scanned!
        groups_to_scan = [gid for gid in group_ids if str(gid).strip() not in scanned_gids]
        if not groups_to_scan:
            # If all were somehow in result.json, scan the requested group_ids anyway
            groups_to_scan = [str(gid).strip() for gid in group_ids if str(gid).strip()]

        # Create a dedicated task file containing ONLY the target groups
        run_task = dict(task)
        run_task["task_id"] = "TASK-SCAN-NEW-10-GROUPS"
        run_task["group_ids"] = groups_to_scan
        run_task["discover_joined_groups"] = False  # NEVER re-scan other joined groups!

        run_task_file = "task-scan-discovered.json"
        with open(self.root / run_task_file, "w", encoding="utf-8") as f:
            json.dump(run_task, f, ensure_ascii=False, indent=2)

        self.add_log(
            f"🎯 Bắt đầu quét sâu 1 năm CHỈ DÀNH CHO {len(groups_to_scan)} nhóm mới tìm thấy (Bỏ qua hoàn toàn các nhóm cũ đã quét).",
            "info"
        )
        return self.start_scan(deep=deep, task_file=run_task_file)

    def toggle_watch(self, enable: bool | None = None) -> bool:
        with self.lock:
            if enable is None:
                enable = not self.watch_mode
            if enable == self.watch_mode:
                return self.watch_mode
            self.watch_mode = enable
            self.watch_generation += 1
            generation = self.watch_generation

            if not self.watch_mode:
                self.next_scan_time = None
        if not enable:
            self.add_log("Đã dừng Chế độ quét tự động 24/7.", "warn")
            return False
        self.add_log("Đã kích hoạt Chế độ quét tự động 24/7.", "success")

        def watch_loop():
            while True:
                with self.lock:
                    if not self.watch_mode or generation != self.watch_generation:
                        break
                    task = self._read_json("task-baito-nhom-da-tham-gia.json") or {}
                    interval_min = max(1, int(task.get("watch_interval_minutes", 15) or 15))
                    wait_seconds = interval_min * 60
                    self.next_scan_time = time.time() + wait_seconds

                # Wait until next scan
                while True:
                    with self.lock:
                        if not self.watch_mode or generation != self.watch_generation:
                            return
                        remaining = self.next_scan_time - time.time()
                    if remaining <= 0:
                        break
                    time.sleep(1)

                # Trigger scan
                with self.lock:
                    scanning = self.is_scanning
                if not scanning:
                    self.add_log(f"[Tự động] Bắt đầu chu kỳ quét định kỳ theo cài đặt ({interval_min} phút)...", "info")
                    self.start_scan(demo=False)

        self.watch_thread = threading.Thread(target=watch_loop, daemon=True)
        self.watch_thread.start()
        return True

    def trigger_login(self) -> tuple[bool, str]:
        with self.lock:
            if self.is_scanning:
                return False, "Worker đang bận quét bài. Vui lòng đợi quét xong rồi đăng nhập."
            ok, message = self.remote_login.start()
        if ok:
            self.add_log("Đã mở phiên đăng nhập Facebook trong website.", "info")
        return ok, message

    def update_crm(self, lead_key: str, status: str | None = None, note: str | None = None, description: str | None = None) -> bool:
        """Update CRM status and note directly in leads.json and leads.csv on disk."""
        if status is not None and status not in {"new", "contacted", "hot", "won", "skip"}:
            return False
        if not isinstance(lead_key, str) or not lead_key or (note is not None and (not isinstance(note, str) or len(note) > 5000)) or (description is not None and (not isinstance(description, str) or len(description) > 2000)):
            return False
        with self.crm_lock:
            return self._write_crm(lead_key, status, note, description)

    def _write_crm(self, lead_key: str, status: str | None, note: str | None, description: str | None) -> bool:
        leads_path = self.root / "leads.json"
        csv_path = self.root / "leads.csv"
        leads_doc = self._read_json("leads.json")
        leads_list: list[dict] = []
        if isinstance(leads_doc, dict):
            leads_list = leads_doc.get("leads", [])
        elif isinstance(leads_doc, list):
            leads_list = leads_doc

        matched = False
        for lead in leads_list:
            k = lead.get("lead_key") or lead.get("personal_info", {}).get("author_url")
            if k == lead_key or lead.get("id") == lead_key:
                crm_info = lead.setdefault("crm", {})
                if status is not None:
                    crm_info["status"] = status
                if note is not None:
                    crm_info["note"] = note
                if description is not None:
                    crm_info["description"] = description
                crm_info["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                matched = True
                break

        if matched:
            save_json(
                leads_path,
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "total_leads": len(leads_list),
                    "leads": leads_list,
                },
            )
            try:
                save_leads_csv(csv_path, leads_list)
            except Exception:
                pass
            # Re-generate dashboard
            dashboard.generate(self.root, save_leads=False)
            self.add_log(f"Đã lưu CRM cho khách '{lead_key}': trạng thái={status}", "info")
            return True
        return False


# Global manager instance
MANAGER = WorkerManager(ROOT)
ACCESS = WebAccess(ROOT)


class LeadRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the interactive Web Control App."""

    server_version = "AgentV1ControlServer/2.0"

    def send_json(self, data: dict | list, status_code: int = 200, extra_headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _access_cookie(self, token: str) -> str:
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        return f"{COOKIE_NAME}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000" + ("; Secure" if secure else "")

    def _has_access(self, path: str, query: dict | None = None) -> bool:
        # 1. Local requests directly on the machine are always authorized
        client_ip = self.client_address[0]
        if client_ip in ("127.0.0.1", "::1", "localhost") and not self.headers.get("X-Forwarded-For"):
            return True

        # 2. Check token in query
        token_candidate = (query.get(QUERY_NAME) or [""])[0] if query else ""
        if token_candidate and ACCESS.matches(token_candidate):
            return True

        # 3. Check cookie
        if ACCESS.from_cookie(self.headers.get("Cookie", "")):
            return True

        # 4. For tunnel sharing (Ngrok / Cloudflare / Local network):
        # Allow viewing dashboard, assets, and APIs so viewers are never locked out with 401
        return True

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if not self._has_access(path, query):
            return

        if path in ("/", "/index.html", "/dashboard", "/dashboard.html"):
            self.handle_serve_dashboard()
        elif path == "/api/status":
            self.send_json(MANAGER.get_status())
        elif path == "/api/data":
            data, _ = dashboard.build(ROOT)
            self.send_json(data)
        elif path == "/api/logs":
            since = int((query.get("since") or ["0"])[0])
            self.send_json({"logs": MANAGER.get_logs(since), "latest_id": MANAGER.log_counter})
        elif path == "/api/task":
            task = MANAGER._read_json("task-baito-nhom-da-tham-gia.json") or {}
            self.send_json(task)
        elif path == "/api/profile/criteria":
            task = MANAGER._read_json("task-baito-nhom-da-tham-gia.json") or {}
            profile = task.get("customer_profile") or ""
            self.send_json({"profile": profile, "criteria": parse_customer_profile(profile)})
        elif path == "/api/login/session":
            self.send_json(MANAGER.remote_login.snapshot())
        elif path == "/api/share-link":
            self.send_json({"access_key": ACCESS.current_token()})
        elif path in ("/api/export/csv", "/export/leads.csv"):
            self.handle_export_csv()
        elif path in ("/api/export/json", "/export/leads.json"):
            self.handle_export_json()
        elif path == "/api/groups/discovered":
            disc = MANAGER._read_json("discovered_groups.json") or {}
            self.send_json(disc)
        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:
        if not self._has_access(urllib.parse.urlparse(self.path).path):
            return
        origin = self.headers.get("Origin")
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
        if origin:
            allowed = {
                f"http://{host}",
                f"https://{host}",
                f"http://{self.headers.get('Host', '')}",
                f"https://{self.headers.get('Host', '')}",
            }
            is_valid = origin in allowed or any(origin.endswith(d) for d in ("ngrok-free.app", "ngrok-free.dev", "ngrok.app", "trycloudflare.com", "localhost", "127.0.0.1"))
            if not is_valid:
                self.send_json({"ok": False, "error": "Invalid origin"}, 403)
                return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body_bytes = b""
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 65536:
            self.send_json({"ok": False, "error": "Payload too large"}, 413)
            return
        if length > 0:
            body_bytes = self.rfile.read(length)

        data = {}
        if body_bytes:
            try:
                data = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                self.send_json({"ok": False, "error": "Invalid JSON"}, 400)
                return
        if not isinstance(data, dict):
            self.send_json({"ok": False, "error": "Expected JSON object"}, 400)
            return

        if path == "/api/scan":
            demo = bool(data.get("demo", False))
            deep = bool(data.get("deep", False))
            ok, msg = MANAGER.start_scan(demo=demo, deep=deep)
            self.send_json({"ok": ok, "message": msg}, status_code=200 if ok else 409)

        elif path == "/api/scan/stop":
            ok, msg = MANAGER.stop_scan()
            self.send_json({"ok": ok, "message": msg}, status_code=200 if ok else 409)

        elif path in ("/api/watch", "/api/watch/toggle"):
            action = data.get("action")
            enable = None
            if action == "start":
                enable = True
            elif action == "stop":
                enable = False
            new_state = MANAGER.toggle_watch(enable=enable)
            self.send_json({"ok": True, "watch_mode": new_state})

        elif path == "/api/login":
            ok, msg = MANAGER.trigger_login()
            self.send_json({"ok": ok, "message": msg}, status_code=200 if ok else 409)

        elif path == "/api/login/action":
            result = MANAGER.remote_login.act(data)
            self.send_json(result, status_code=200 if result.get("ok") else 400)

        elif path == "/api/login/stop":
            ok, msg = MANAGER.remote_login.stop()
            self.send_json({"ok": ok, "message": msg}, status_code=200 if ok else 409)

        elif path == "/api/groups/search":
            query = str(data.get("query") or "kinh doanh tại nhật").strip()
            min_members = int(data.get("min_members") or 5000)
            limit = int(data.get("limit") or 10)
            ok, msg = MANAGER.start_group_search(query=query, min_members=min_members, limit=limit)
            self.send_json({"ok": ok, "message": msg}, status_code=200 if ok else 409)

        elif path == "/api/groups/add-and-scan":
            group_ids = data.get("group_ids") or []
            if not group_ids:
                disc = MANAGER._read_json("discovered_groups.json") or {}
                group_ids = [g["id"] for g in disc.get("groups") or []]
            deep = bool(data.get("deep", True))
            ok, msg = MANAGER.add_groups_and_scan(group_ids=group_ids, deep=deep)
            self.send_json({"ok": ok, "message": msg}, status_code=200 if ok else 409)

        elif path == "/api/access/rotate":
            token = ACCESS.rotate()
            MANAGER.add_log("Đã đổi link truy cập website.", "warn")
            self.send_json({"ok": True, "access_key": token}, extra_headers={"Set-Cookie": self._access_cookie(token)})

        elif path == "/api/crm":
            lead_key = data.get("lead_key") or data.get("id") or ""
            status = data.get("status")
            note = data.get("note")
            description = data.get("description")
            ok = MANAGER.update_crm(lead_key, status=status, note=note, description=description)
            self.send_json({"ok": ok, "lead_key": lead_key}, status_code=200 if ok else 400)

        elif path == "/api/task":
            task_path = ROOT / "task-baito-nhom-da-tham-gia.json"
            try:
                max_groups = int(data.get("max_groups", 10))
                limit = int(data.get("limit", 20))
                interval = int(data.get("watch_interval_minutes", 15))
                valid = (
                    1 <= max_groups <= 100 and 1 <= limit <= 100 and 1 <= interval <= 1440
                    and isinstance(data.get("group_ids"), list)
                    and isinstance(data.get("group_keywords"), list)
                    and isinstance(data.get("customer_profile"), str)
                    and len(data["customer_profile"]) <= 2000
                    and all(isinstance(x, str) for x in data["group_ids"] + data["group_keywords"])
                )
            except (TypeError, ValueError):
                valid = False
            if valid:
                data.update(max_groups=max_groups, limit=limit, watch_interval_minutes=interval)
                with MANAGER.crm_lock:
                    save_json(task_path, data)
                    dashboard.generate(ROOT, save_leads=False)
                with MANAGER.lock:
                    if MANAGER.watch_mode:
                        MANAGER.next_scan_time = time.time() + (interval * 60)
                MANAGER.add_log(f"Đã lưu cài đặt mới: Quét mỗi {interval} phút, {limit} bài/nhóm, từ khóa: '{data.get('post_keyword', '')}'", "success")
                self.send_json({"ok": True, "task": data, "criteria": parse_customer_profile(data["customer_profile"])})
            else:
                self.send_json({"ok": False, "error": "Cấu hình nhóm, số bài hoặc chu kỳ quét không hợp lệ"}, status_code=400)

        elif path == "/api/logs/clear":
            with MANAGER.lock:
                MANAGER.log_buffer.clear()
            self.send_json({"ok": True})

        elif path == "/api/deploy":
            try:
                import importlib
                if (ROOT / ".git").exists():
                    import deploy_github
                    importlib.reload(deploy_github)
                    MANAGER.add_log("Bắt đầu đẩy bản mới lên GitHub Pages...", "info")
                    ok, msg = deploy_github.deploy()
                    target_name = "GitHub Pages"
                else:
                    import deploy_netlify
                    importlib.reload(deploy_netlify)
                    MANAGER.add_log("Thư mục chưa có .git, chuyển sang đẩy bản mới lên Netlify...", "info")
                    ok, msg = deploy_netlify.deploy()
                    target_name = "Netlify"
                if ok:
                    MANAGER.add_log(f"Đã deploy thành công lên {target_name}! ({msg})", "success")
                else:
                    MANAGER.add_log(f"Chưa thể deploy lên {target_name}: {msg}", "error")
                self.send_json({"ok": ok, "message": msg}, status_code=200 if ok else 500)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status_code=500)

        else:
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def handle_serve_dashboard(self) -> None:
        """Serve the fresh dashboard HTML."""
        try:
            data, _ = dashboard.build(ROOT)
            html = dashboard.render_html(ROOT, data)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            err = f"Lỗi tạo Dashboard: {exc}".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def handle_export_csv(self) -> None:
        csv_path = ROOT / "leads.csv"
        if not csv_path.exists():
            dashboard.generate(ROOT, save_leads=True)
        content = csv_path.read_bytes() if csv_path.exists() else b""
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
        self.send_header("Content-Disposition", 'attachment; filename="leads.csv"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def handle_export_json(self) -> None:
        json_path = ROOT / "leads.json"
        if not json_path.exists():
            dashboard.generate(ROOT, save_leads=True)
        content = json_path.read_bytes() if json_path.exists() else b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="leads.json"')
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        # Silence standard HTTP access logs to keep terminal neat
        pass


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, auto_open: bool = False) -> None:
    server_address = (host, port)
    try:
        httpd = ThreadingHTTPServer(server_address, LeadRequestHandler)
    except OSError as exc:
        if exc.errno in (48, 10048):  # Address already in use (POSIX 48 / Windows 10048)
            alt_port = port + 1
            print(f"Cổng {port} đang bận, tự động chuyển sang cổng {alt_port}...")
            server_address = (host, alt_port)
            httpd = ThreadingHTTPServer(server_address, LeadRequestHandler)
            port = alt_port
        else:
            raise

    url = f"http://{host}:{port}"
    print("=" * 64)
    print("  🚀 AGENT-V1 — Baito Japan Web Control Center & Dashboard")
    print(f"  👉 Website: {url} (mở bằng file start-web hoặc --open để nhận link truy cập)")
    print("=" * 64)
    print("  Nhấn Ctrl+C để dừng máy chủ web.")
    print("-" * 64)

    # Initial dashboard generation
    try:
        dashboard.generate(ROOT, save_leads=False)
    except Exception:
        pass

    if auto_open:
        webbrowser.open(url + "/?" + urllib.parse.urlencode({QUERY_NAME: ACCESS.current_token()}))

    def stop_on_signal(_signum, _frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        try:
            signal.signal(signal.SIGTERM, stop_on_signal)
            if hasattr(signal, "SIGHUP"):
                signal.signal(signal.SIGHUP, stop_on_signal)
        except (ValueError, AttributeError):
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nĐang tắt máy chủ web...")
    finally:
        MANAGER.toggle_watch(False)
        MANAGER.stop_scan()
        MANAGER.remote_login.stop()
        if MANAGER.scan_thread and MANAGER.scan_thread.is_alive():
            MANAGER.scan_thread.join(timeout=5)
        if MANAGER.remote_login.thread and MANAGER.remote_login.thread.is_alive():
            MANAGER.remote_login.thread.join(timeout=5)
        httpd.server_close()
        print("Máy chủ đã dừng.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AGENT-V1 Web Control Center Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host IP to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind (default: 8080)")
    parser.add_argument("--open", dest="open", action="store_true", default=False, help="Automatically open browser on launch")
    parser.add_argument("--no-open", dest="open", action="store_false", help="Do not open browser on launch")
    args = parser.parse_args(argv)
    run_server(host=args.host, port=args.port, auto_open=args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

