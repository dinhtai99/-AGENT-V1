"""Interactive Facebook login controlled through the web dashboard.

Playwright's synchronous API stays on one thread. HTTP handlers exchange small,
validated commands with that thread; credentials are never written to logs.
"""

from __future__ import annotations

import base64
import os
import queue
import threading
import time
from pathlib import Path


VIEWPORT = {"width": 1100, "height": 760}
ALLOWED_KEYS = {"Tab", "Enter", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"}
ACTIVE_STATES = {"starting", "ready", "stopping"}


class RemoteLoginSession:
    def __init__(self, state_path: Path, start_url: str = "https://www.facebook.com/login"):
        self.state_path = Path(state_path)
        self.start_url = start_url
        self.lock = threading.Lock()
        self.commands: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.state = "idle"
        self.message = "Chưa bắt đầu đăng nhập."

    def _set_state(self, state: str, message: str) -> None:
        with self.lock:
            self.state = state
            self.message = message

    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "message": self.message}

    def is_active(self) -> bool:
        with self.lock:
            return self.state in ACTIVE_STATES

    def start(self) -> tuple[bool, str]:
        with self.lock:
            if self.state in ACTIVE_STATES:
                return False, "Một phiên đăng nhập đang mở."
            self.state = "starting"
            self.message = "Đang mở Facebook..."
            self.commands = queue.Queue()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        return True, "Đã mở phiên đăng nhập Facebook trên website."

    def _request(self, command: dict, timeout: float = 12) -> dict:
        if not self.is_active():
            return {"ok": False, **self.status()}
        response: queue.Queue = queue.Queue(maxsize=1)
        self.commands.put((command, response))
        try:
            return response.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, **self.status(), "message": "Trình duyệt chưa phản hồi. Hãy thử lại."}

    def snapshot(self) -> dict:
        current = self.status()
        if current["state"] != "ready":
            return current
        return self._request({"action": "snapshot"})

    def act(self, data: dict) -> dict:
        action = data.get("action")
        if action == "click":
            x, y = data.get("x"), data.get("y")
            if not isinstance(x, int) or not isinstance(y, int) or not (0 <= x < VIEWPORT["width"] and 0 <= y < VIEWPORT["height"]):
                return {"ok": False, "message": "Vị trí bấm không hợp lệ."}
            command = {"action": "click", "x": x, "y": y}
        elif action == "type":
            value = data.get("text")
            if not isinstance(value, str) or not (1 <= len(value) <= 1000):
                return {"ok": False, "message": "Nội dung nhập không hợp lệ."}
            command = {"action": "type", "text": value}
        elif action == "key":
            value = data.get("key")
            if value not in ALLOWED_KEYS:
                return {"ok": False, "message": "Phím không được hỗ trợ."}
            command = {"action": "key", "key": value}
        elif action == "scroll":
            value = data.get("dy")
            if not isinstance(value, int) or abs(value) > 1500:
                return {"ok": False, "message": "Khoảng cuộn không hợp lệ."}
            command = {"action": "scroll", "dy": value}
        else:
            return {"ok": False, "message": "Thao tác không được hỗ trợ."}
        return self._request(command)

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if self.state not in ACTIVE_STATES:
                return False, "Không có phiên đăng nhập đang mở."
            self.state = "stopping"
            self.message = "Đang đóng phiên đăng nhập..."
            self.commands.put(({"action": "stop"}, None))
        return True, "Đang đóng phiên đăng nhập."

    def _run(self) -> None:
        browser = None
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                # A headed browser keeps Facebook's existing login behaviour on
                # the host machine; the dashboard mirrors it for the remote user.
                headless = os.environ.get("AGENT_LOGIN_HEADLESS") == "1"
                browser = playwright.chromium.launch(headless=headless)
                context = browser.new_context(viewport=VIEWPORT)
                page = context.new_page()
                page.goto(self.start_url, wait_until="domcontentloaded", timeout=30000)
                self._set_state("ready", "Bấm vào ô trên ảnh, nhập nội dung ở bên dưới rồi bấm Gõ.")
                deadline = time.monotonic() + 600

                while time.monotonic() < deadline:
                    current_url = page.url.lower()
                    logged_in = any(cookie.get("name") == "c_user" for cookie in context.cookies())
                    if logged_in and "checkpoint" not in current_url and "login" not in current_url:
                        temp_path = self.state_path.with_suffix(".login.tmp")
                        context.storage_state(path=str(temp_path))
                        os.chmod(temp_path, 0o600)
                        temp_path.replace(self.state_path)
                        self._set_state("saved", "Đăng nhập thành công. Phiên Facebook đã được lưu.")
                        break
                    try:
                        command, response = self.commands.get(timeout=0.4)
                    except queue.Empty:
                        continue
                    action = command["action"]
                    if action == "stop":
                        self._set_state("stopped", "Đã đóng phiên đăng nhập.")
                        break
                    try:
                        if action == "snapshot":
                            shot = page.screenshot(type="jpeg", quality=65, timeout=8000)
                            result = {"ok": True, "state": "ready", "message": self.status()["message"],
                                      "image": base64.b64encode(shot).decode("ascii"), "width": VIEWPORT["width"], "height": VIEWPORT["height"]}
                        elif action == "click":
                            page.mouse.click(command["x"], command["y"])
                            result = {"ok": True}
                        elif action == "type":
                            page.keyboard.insert_text(command["text"])
                            result = {"ok": True}
                        elif action == "key":
                            page.keyboard.press(command["key"])
                            result = {"ok": True}
                        else:
                            page.mouse.wheel(0, command["dy"])
                            result = {"ok": True}
                    except Exception:
                        result = {"ok": False, "message": "Không thực hiện được thao tác. Hãy thử lại."}
                    if response is not None:
                        response.put(result)
                else:
                    self._set_state("error", "Hết 10 phút chờ đăng nhập Facebook. Hãy bắt đầu lại.")
        except ModuleNotFoundError:
            self._set_state("error", "Playwright chưa được cài cho máy chủ web.")
        except Exception as exc:
            message = "Chromium chưa được cài. Hãy chạy playwright install chromium." if "Executable doesn't exist" in str(exc) else "Không mở được Facebook trong Chromium. Hãy kiểm tra trình duyệt và kết nối mạng."
            self._set_state("error", message)
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass
