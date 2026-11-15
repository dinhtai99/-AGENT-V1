"""Passwordless access links for the local web control server."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
from http.cookies import SimpleCookie
from pathlib import Path


COOKIE_NAME = "agentv1_access"
QUERY_NAME = "access_key"


class WebAccess:
    def __init__(self, root: Path):
        self.path = Path(root) / ".web-access-key"
        self.lock = threading.Lock()
        if not self.path.exists():
            self._create()
        self.token = self.path.read_text(encoding="ascii").strip()
        if len(self.token) < 32:
            raise RuntimeError("Khóa truy cập web không hợp lệ. Hãy tạo lại khóa.")

    def _create(self) -> None:
        token = secrets.token_urlsafe(32)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(token + "\n")

    def matches(self, candidate: str) -> bool:
        with self.lock:
            return bool(candidate) and hmac.compare_digest(candidate, self.token)

    def from_cookie(self, raw_cookie: str) -> bool:
        try:
            cookies = SimpleCookie()
            cookies.load(raw_cookie)
            entry = cookies.get(COOKIE_NAME)
            return self.matches(entry.value if entry else "")
        except Exception:
            return False

    def current_token(self) -> str:
        with self.lock:
            return self.token

    def rotate(self) -> str:
        token = secrets.token_urlsafe(32)
        temp_path = self.path.with_suffix(".new")
        with self.lock:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(token + "\n")
            os.replace(temp_path, self.path)
            self.token = token
        return token
