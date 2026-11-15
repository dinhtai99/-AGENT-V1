#!/usr/bin/env python3
"""Run the local web server and a temporary HTTPS tunnel together.

The tunnel and web server live only while this process is running. The
shareable URL changes at every start and includes the existing access key.
"""

from __future__ import annotations

import collections
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from web_access import COOKIE_NAME, QUERY_NAME, WebAccess


ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8080
PUBLIC_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b")

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def port_is_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, PORT))
            return True
        except OSError:
            return False


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def collect_output(proc: subprocess.Popen, lines: collections.deque, urls: queue.Queue) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        clean = line.rstrip()
        lines.append(clean)
        match = PUBLIC_URL_RE.search(clean)
        if match:
            urls.put(match.group(0))


def wait_for_server(proc: subprocess.Popen, token: str, timeout: int = 20) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        request = urllib.request.Request(
            f"http://{HOST}:{PORT}/api/status",
            headers={"Cookie": f"{COOKIE_NAME}={token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return True
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.2)
    return False


def main() -> int:
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        print("Chưa tìm thấy cloudflared. Hãy cài Cloudflare Tunnel trước khi chia sẻ.")
        return 1
    if not port_is_free():
        print("Cổng 8080 đang được dùng. Hãy tắt cửa sổ web đang chạy rồi mở lại file này.")
        return 1

    token = WebAccess(ROOT).current_token()
    server = None
    tunnel = None
    server_lines: collections.deque[str] = collections.deque(maxlen=30)
    tunnel_lines: collections.deque[str] = collections.deque(maxlen=30)
    urls: queue.Queue[str] = queue.Queue()

    def stop_on_signal(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_on_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop_on_signal)

    try:
        import os
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        server = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "server.py"), "--host", HOST, "--port", str(PORT)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env,
        )
        threading.Thread(target=collect_output, args=(server, server_lines, queue.Queue()), daemon=True).start()
        if not wait_for_server(server, token):
            print("Máy chủ web không khởi động được.")
            for line in list(server_lines)[-8:]:
                print(line)
            return 1

        print("Website trên máy đã sẵn sàng. Đang tạo link HTTPS tạm thời…", flush=True)
        tunnel = subprocess.Popen(
            [cloudflared, "tunnel", "--no-autoupdate", "--url", f"http://{HOST}:{PORT}"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=collect_output, args=(tunnel, tunnel_lines, urls), daemon=True).start()
        public_url = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if server.poll() is not None or tunnel.poll() is not None:
                break
            try:
                public_url = urls.get(timeout=0.5)
                break
            except queue.Empty:
                continue
        if not public_url:
            print("Không tạo được link chia sẻ.")
            for line in list(tunnel_lines)[-8:]:
                print(line)
            return 1

        share_url = public_url + "/?" + urllib.parse.urlencode({QUERY_NAME: token})
        local_url = f"http://{HOST}:{PORT}/?" + urllib.parse.urlencode({QUERY_NAME: token})
        print("\nLINK GỬI CHO BẠN CỦA BẠN:", share_url, sep="\n", flush=True)
        if shutil.which("pbcopy"):
            try:
                subprocess.run(["pbcopy"], input=share_url, text=True, check=True, timeout=3)
                print("Link đã được sao chép vào bộ nhớ tạm.")
            except (OSError, subprocess.SubprocessError):
                pass
        elif shutil.which("clip"):
            try:
                subprocess.run(["clip"], input=share_url, text=True, check=True, timeout=3)
                print("Link đã được sao chép vào bộ nhớ tạm.")
            except (OSError, subprocess.SubprocessError):
                pass
        webbrowser.open(local_url)
        print("Giữ cửa sổ này mở để website hoạt động. Bấm Ctrl+C để tắt cả link và máy chủ.", flush=True)
        print("Mỗi lần bật sẽ có link mới; hãy gửi lại link mới cho người dùng.", flush=True)
        while server.poll() is None and tunnel.poll() is None:
            time.sleep(0.5)
        print("Website hoặc đường kết nối đã dừng.")
        return 1
    except KeyboardInterrupt:
        print("\nĐang tắt link chia sẻ và máy chủ web…", flush=True)
        return 0
    finally:
        stop_process(tunnel)
        stop_process(server)
        print("Đã tắt.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
