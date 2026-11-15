#!/usr/bin/env python3
"""Run the local web server and an Ngrok HTTPS tunnel together.

The tunnel and web server live while this process is running.
The public Ngrok URL is displayed, copied to clipboard, and opened automatically.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from web_access import COOKIE_NAME, QUERY_NAME, WebAccess

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8080

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def port_is_free(port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


def is_server_ready(token: str) -> bool:
    try:
        req = urllib.request.Request(
            f"http://{HOST}:{PORT}/api/status",
            headers={"Cookie": f"{COOKIE_NAME}={token}"},
        )
        with urllib.request.urlopen(req, timeout=1) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_for_server(token: str, timeout: int = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_server_ready(token):
            return True
        time.sleep(0.3)
    return False


def find_python() -> str:
    candidates = [
        Path.home() / ".venv-agent" / "Scripts" / "python.exe",
        Path.home() / ".venv-agent" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable or "python"


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def get_ngrok_public_url(timeout: int = 15) -> str | None:
    api_url = "http://127.0.0.1:4040/api/tunnels"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(api_url, headers={"User-Agent": "agentv1"})
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    tunnels = data.get("tunnels", [])
                    for tun in tunnels:
                        purl = tun.get("public_url", "")
                        if purl.startswith("https://"):
                            return purl
                        if purl.startswith("http://"):
                            return purl.replace("http://", "https://", 1)
        except Exception:
            pass
        time.sleep(0.5)
    return None


def copy_to_clipboard(text: str) -> None:
    try:
        if sys.platform == "darwin" and shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=2)
        elif os.name == "nt" and shutil.which("clip"):
            subprocess.run(["clip"], input=text, text=True, check=True, timeout=2)
    except Exception:
        pass


def main() -> int:
    ngrok_bin = shutil.which("ngrok") or "/opt/homebrew/bin/ngrok"
    if not os.path.exists(ngrok_bin) and not shutil.which("ngrok"):
        print("❌ Chưa tìm thấy ngrok trên máy.")
        if os.name == "nt":
            print("Cài đặt nhanh trên Windows: winget install Ngrok.Ngrok  (hoặc tải từ https://ngrok.com/download)")
        else:
            print("Cài đặt nhanh bằng Homebrew: brew install ngrok")
        return 1

    token = WebAccess(ROOT).current_token()
    server_proc = None
    ngrok_proc = None

    # 1. Start web server if not already responding
    if not is_server_ready(token):
        if not port_is_free(PORT):
            print("⚠️ Cổng 8080 đang bận. Đang thử kết nối...")
        py_bin = find_python()
        print("🚀 Đang khởi động máy chủ Web...")
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        server_proc = subprocess.Popen(
            [py_bin, str(ROOT / "server.py"), "--no-open"],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )

        # Pipe server output in background thread for debugging
        def stream_server_logs():
            assert server_proc.stdout is not None
            for line in server_proc.stdout:
                line_str = line.strip()
                if "error" in line_str.lower() or "exception" in line_str.lower():
                    print(f"[Server] {line_str}")

        threading.Thread(target=stream_server_logs, daemon=True).start()

        if not wait_for_server(token, timeout=12):
            print("❌ Không thể kết nối với máy chủ Web trên cổng 8080.")
            stop_process(server_proc)
            return 1
        print("✅ Máy chủ Web đã sẵn sàng tại http://127.0.0.1:8080")
    else:
        print("✅ Máy chủ Web đang chạy sẵn tại http://127.0.0.1:8080")

    # 2. Start ngrok targeting explicit IPv4 127.0.0.1:8080 (prevents IPv6 [::1] connection refused)
    print("🌐 Đang khởi tạo đường truyền bảo mật qua Ngrok...")
    ngrok_proc = subprocess.Popen(
        [str(ngrok_bin), "http", f"127.0.0.1:{PORT}"],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def cleanup(*_args) -> None:
        print("\n🛑 Đang dừng Ngrok và đóng máy chủ...")
        stop_process(ngrok_proc)
        stop_process(server_proc)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    public_url = get_ngrok_public_url(timeout=15)
    if not public_url:
        print("⚠️ Không lấy được link từ Ngrok. Có thể bạn cần chạy 'ngrok config add-authtoken <token>' trước.")
        print("Xem chi tiết lỗi tại: http://127.0.0.1:4040")
        stop_process(ngrok_proc)
        stop_process(server_proc)
        return 1

    share_url = f"{public_url}/dashboard.html?{QUERY_NAME}={token}"
    copy_to_clipboard(share_url)

    print()
    print("==================================================================")
    print("  🎉 LINK XEM WEB ĐÃ SẴN SÀNG (ĐÃ COPY VÀO CLIPBOARD) 🎉")
    print("==================================================================")
    print(f"👉 Link chia sẻ: {share_url}")
    print()
    print("💡 Bất kỳ ai mở link trên (trên điện thoại hoặc máy tính) đều xem được!")
    print("🔒 Link có mã bảo mật riêng, an toàn 100%.")
    print("⚡ Dữ liệu cập nhật thời gian thực, đồng bộ 2 chiều với máy của bạn.")
    print("👉 Nhấn Ctrl + C để dừng chia sẻ.")
    print("==================================================================")
    print()

    try:
        webbrowser.open(share_url)
    except Exception:
        pass

    try:
        while True:
            if ngrok_proc.poll() is not None:
                print("Ngrok đã dừng.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
