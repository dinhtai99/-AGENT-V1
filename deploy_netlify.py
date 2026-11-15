#!/usr/bin/env python3
"""Automatically deploy dist.zip to Netlify via Netlify REST API.

No external dependencies needed — uses Python standard library.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / ".netlify-config.json"

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "site_id": "stalwart-halva-7d6aae.netlify.app",
        "auth_token": os.getenv("NETLIFY_AUTH_TOKEN", ""),
    }


def save_config(site_id: str, auth_token: str, uuid: str = "") -> None:
    data = {
        "site_id": site_id.strip(),
        "auth_token": auth_token.strip(),
    }
    if uuid:
        data["uuid"] = uuid.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clean_site_name(raw: str) -> str:
    s = raw.strip()
    if s.endswith(".netlify.app"):
        s = s[:-len(".netlify.app")]
    s = s.replace("https://", "").replace("http://", "").strip("/")
    return s


def resolve_site_uuid(token: str, raw_target: str) -> str | None:
    """Query Netlify API /sites to find the exact site UUID matching our site name."""
    clean_target = clean_site_name(raw_target).lower()
    url = "https://api.netlify.com/api/v1/sites"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "AGENT-V1-AutoDeploy",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status == 200:
                sites = json.loads(resp.read().decode("utf-8"))
                for s in sites:
                    s_id = s.get("id") or s.get("site_id") or ""
                    s_name = (s.get("name") or "").lower()
                    s_url = (s.get("url") or "").lower()
                    if clean_target == s_name or clean_target in s_url or clean_target in s_id.lower():
                        return s_id
                if sites:
                    # Fallback to the first site in account
                    return sites[0].get("id")
    except Exception:
        pass
    return None


def deploy(zip_path: Path | None = None) -> tuple[bool, str]:
    import zipfile
    if zip_path is None:
        zip_path = ROOT / "dist.zip"

    # Always ensure dist/ has index.html, _headers, and _redirects
    dist_dir = ROOT / "dist"
    dist_dir.mkdir(exist_ok=True)
    index_file = dist_dir / "index.html"
    if (ROOT / "dashboard.html").exists():
        index_file.write_text((ROOT / "dashboard.html").read_text(encoding="utf-8"), encoding="utf-8")

    headers_file = dist_dir / "_headers"
    headers_file.write_text("/*\n  Content-Type: text/html; charset=UTF-8\n  X-Frame-Options: SAMEORIGIN\n  X-Content-Type-Options: nosniff\n", encoding="utf-8")

    redirects_file = dist_dir / "_redirects"
    redirects_file.write_text("/*    /index.html   200\n", encoding="utf-8")

    # Repackage dist.zip with HTML + headers + redirects
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(index_file, arcname="index.html")
        zf.write(headers_file, arcname="_headers")
        zf.write(redirects_file, arcname="_redirects")

    cfg = load_config()
    raw_site = cfg.get("site_id", "").strip() or "stalwart-halva-7d6aae.netlify.app"
    token = cfg.get("auth_token", "").strip()
    cached_uuid = cfg.get("uuid", "").strip()

    if not token:
        return False, (
            "Chưa có Netlify Token. Hãy thêm token vào file .netlify-config.json"
        )

    # 1. Resolve site identifier
    candidates = []
    if cached_uuid:
        candidates.append(cached_uuid)
    
    # Try resolving via API
    resolved_id = resolve_site_uuid(token, raw_site)
    if resolved_id and resolved_id not in candidates:
        candidates.insert(0, resolved_id)
        # Update cached UUID
        save_config(raw_site, token, uuid=resolved_id)

    # Fallback candidates: clean subdomain, and raw site
    clean_name = clean_site_name(raw_site)
    if clean_name not in candidates:
        candidates.append(clean_name)
    if raw_site not in candidates:
        candidates.append(raw_site)

    data_bytes = zip_path.read_bytes()
    last_err = ""

    # Try each candidate identifier
    for target in candidates:
        deploy_url = f"https://api.netlify.com/api/v1/sites/{target}/deploys"
        req = urllib.request.Request(
            deploy_url,
            data=data_bytes,
            headers={
                "Content-Type": "application/zip",
                "Authorization": f"Bearer {token}",
                "User-Agent": "AGENT-V1-AutoDeploy",
            },
            method="POST",
        )

        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status in (200, 201):
                        res_json = json.loads(resp.read().decode("utf-8"))
                        ssl_url = res_json.get("ssl_url") or res_json.get("url") or f"https://{clean_name}.netlify.app"
                        # Save winning UUID
                        actual_id = res_json.get("site_id") or target
                        save_config(raw_site, token, uuid=actual_id)
                        return True, f"Đã deploy thành công lên Netlify: {ssl_url}"
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")
                last_err = f"Lỗi Netlify ({exc.code}): {err_body}"
                if exc.code == 404:
                    # 404 means this candidate name was not found, try next candidate
                    break
                time.sleep(2)
            except Exception as exc:
                last_err = f"Lỗi kết nối Netlify: {exc}"
                time.sleep(2)

    return False, last_err or "Không thể deploy lên Netlify sau các lần thử."


def main() -> int:
    cfg = load_config()
    token = cfg.get("auth_token", "")
    site_id = cfg.get("site_id", "stalwart-halva-7d6aae.netlify.app")

    if len(sys.argv) > 1 and sys.argv[1] == "--config":
        new_token = input("Nhập Netlify Personal Access Token: ").strip()
        new_site = input(f"Nhập Site ID/Domain [{site_id}]: ").strip() or site_id
        save_config(new_site, new_token)
        print("✅ Đã lưu cấu hình Netlify vào .netlify-config.json")
        return 0

    if not token:
        print("⚠️ Chưa có Netlify Personal Access Token.")
        print("1. Đăng nhập Netlify -> Bấm avatar góc trên phải -> 'User settings'")
        print("2. Vào 'Applications' -> 'Personal access tokens' -> Bấm 'New access token'")
        print("3. Copy mã token và chạy: python3 deploy_netlify.py --config")
        return 1

    print("🚀 Đang tự động nén và đẩy bản mới lên Netlify...")
    ok, msg = deploy()
    if ok:
        print(f"🎉 {msg}")
        return 0
    else:
        print(f"❌ {msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
