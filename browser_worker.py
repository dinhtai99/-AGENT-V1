#!/usr/bin/env python3
"""
AGENT-V1 — Browser worker (Baito Japan Small Business Lead Finder).

Reads Facebook Groups through a real logged-in browser:
1. Automatically discovers Baito groups that the logged-in Facebook
   account has already joined (`discover_joined_groups: true` or `--list-groups`),
   or scans a provided list of `group_ids`.
2. Filters posts in Baito groups to keep ONLY Small Business Owners /
   Individual Shop Owners (< 20 people) and drop job seekers or large
   recruitment/haken agencies.
3. Extracts Personal & Contact Info (`author_name`, `author_url`,
   `group_member_url`, Japanese/VN phone, Line, Zalo, Inbox).
4. Profiles Step 1 (What business they run: restaurant, izakaya, nail/spa,
   grocery, bento factory, online shop, station/area in Japan, scale < 20)
   and Step 2 (Deep analysis of what they need: roles, shifts, wage,
   customer segment, operational pain points).
5. Outputs `result.json` plus a cumulative deduplicated owner list in
   `leads.json` and Excel-ready `leads.csv`.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).parent))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from filters import (  # noqa: E402
    analyze_business_and_needs,
    build_leads_list,
    clean_fb_noise,
    contains_vn,
    extract_fields,
    in_date_range,
    load_json,
    matches_any_keyword,
    norm_vn,
    post_matches_filters,
    save_json,
    save_leads_csv,
)

MAX_CONSECUTIVE_ERRORS = 3
_SESSION_MARKERS = ("login/checkpoint", "Missing login state")

DEFAULT_BAITO_GROUP_KEYWORDS = [
    "baito",
    "バイト",
    "arubaito",
    "viec lam",
    "tuyen dung",
    "lam them",
    "part time",
    "part-time",
    "quan an",
    "nha hang",
    "tokyo",
    "osaka",
    "kansai",
    "kanto",
    "saitama",
    "chiba",
    "nagoya",
    "aichi",
    "fukuoka",
    "nhat ban",
    "tai nhat",
]

_RESERVED_GROUP_SLUGS = {
    "feed",
    "discover",
    "joins",
    "create",
    "notifications",
    "search",
    "settings",
    "categorized_groups",
    "badges",
    "files",
    "members",
    "people",
    "about",
    "discussion",
    "buy_sell_discussion",
}


def _is_session_error(msg: str) -> bool:
    """True when a scan error means the login session expired."""
    return any(m in (msg or "") for m in _SESSION_MARKERS)


def _jittered(base: float, pct: float) -> float:
    """Randomize a wait by +/-pct% so scans don't run on a mechanical clock."""
    if pct <= 0 or base <= 0:
        return float(base)
    span = base * min(pct, 50) / 100.0
    return base + random.uniform(-span, span)


def _write_status(root: Path, data: dict) -> None:
    """Best-effort: write watch-status.json (heartbeat + last scan state)."""
    try:
        save_json(root / "watch-status.json", data)
    except Exception:
        pass


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -r requirements-browser.txt "
            "then: playwright install chromium"
        ) from exc
    return sync_playwright()


def login(state_path: Path, web_mode: bool = False) -> None:
    """Open a headed browser, let the user log in, then save the session."""
    with _playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")
        if web_mode:
            print("Đăng nhập trong cửa sổ Facebook; phiên sẽ tự lưu sau khi đăng nhập.", flush=True)
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                if any(c.get("name") == "c_user" for c in context.cookies()):
                    break
                page.wait_for_timeout(1000)
            else:
                raise RuntimeError("Hết 5 phút chờ đăng nhập Facebook.")
        else:
            print("Log in to Facebook in the browser window, then press Enter here.")
            input()
        context.storage_state(path=str(state_path))
        browser.close()
    print(f"Saved login state to {state_path}. Keep this file private.")


def _clean_group_name(raw_text: str, fallback_id: str) -> str:
    """Extract clean group name from link text, stripping notification or activity prefixes."""
    lines = [ln.strip() for ln in (raw_text or "").splitlines() if ln.strip()]
    clean_lines: list[str] = []
    for ln in lines:
        low = norm_vn(ln)
        if low in (
            "xem nhom",
            "visit group",
            "da tham gia",
            "joined",
            "ghim",
            "nhom cua ban",
            "hoat dong gan day",
            "chua doc",
            "unread",
        ):
            continue
        if low.startswith(("lan hoat dong", "last active", "ban co ", "co bài viet moi")):
            continue
        # If it's a notification string like "Chưa đọcTừ giờ, bạn có thể đăng bài và bình luận trong Tokyo baito.1 giờ"
        m_notif = re.search(r"bình luận trong\s+(.+?)(?:\.\s*\d+|\.$|$)", ln, re.IGNORECASE)
        if m_notif:
            clean_lines.append(m_notif.group(1).strip())
            continue
        if low.startswith("chua doc"):
            continue
        clean_lines.append(ln)
    return (clean_lines[0] if clean_lines else fallback_id)[:120]


def discover_joined_groups(
    state_path: Path,
    group_keywords: list[str] | None = None,
    max_groups: int = 10,
    debug: bool = False,
) -> list[dict]:
    """Read the logged-in user's joined Facebook groups (`facebook.com/groups/joins/`)
    and filter for Baito / Japan job groups.
    """
    if not state_path.exists():
        raise RuntimeError(
            f"Missing login state {state_path}. Run first: python browser_worker.py --login"
        )
    keywords = (
        DEFAULT_BAITO_GROUP_KEYWORDS
        if group_keywords is None
        else [str(k).strip() for k in group_keywords if str(k).strip()]
    )
    discovered: list[dict] = []
    seen_ids: set[str] = set()

    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()
        try:
            page.goto("https://www.facebook.com/groups/joins/", wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(random.randint(3500, 5500))
        if "login" in page.url or "checkpoint" in page.url:
            browser.close()
            raise RuntimeError(
                "Facebook redirected to a login/checkpoint page. "
                "The saved session may have expired. Run again: "
                "python browser_worker.py --login"
            )
        for _ in range(4):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(random.randint(1500, 2500))

        # Prefer links inside main/navigation, excluding notification popovers
        raw_links = page.locator("div[role='main'] a[href*='/groups/'], div[role='navigation'] a[href*='/groups/']").evaluate_all(
            """els => els.map(e => ({
                href: e.getAttribute('href') || '',
                text: (e.innerText || e.getAttribute('aria-label') || '').trim()
            }))"""
        )
        if not raw_links:
            raw_links = page.locator("a[href*='/groups/']").evaluate_all(
                """els => els.map(e => ({
                    href: e.getAttribute('href') || '',
                    text: (e.innerText || e.getAttribute('aria-label') || '').trim()
                }))"""
            )
        browser.close()

    for item in raw_links:
        href = (item.get("href") or "").strip()
        text = (item.get("text") or "").strip()
        if not href or not text:
            continue
        m = re.search(r"/groups/([^/?#]+)/?", href)
        if not m:
            continue
        gid = m.group(1).strip()
        if not gid or gid.lower() in _RESERVED_GROUP_SLUGS:
            continue
        if re.search(r"/groups/[^/?#]+/(?:posts|permalink|user|members|about|media|files)", href):
            continue
        name = _clean_group_name(text, gid)
        if not name or len(name) < 3 or name == gid:
            continue
        if gid in seen_ids:
            continue
        seen_ids.add(gid)
        discovered.append({
            "id": gid,
            "name": name,
            "url": f"https://www.facebook.com/groups/{gid}/",
        })

    if debug:
        save_json(
            Path(__file__).parent / "debug-groups.json",
            {"total_joined_found": len(discovered), "groups": discovered},
        )

    if keywords:
        matched = [
            g
            for g in discovered
            if any(contains_vn(g["name"], kw) or contains_vn(g["id"], kw) for kw in keywords)
        ]
    else:
        matched = discovered

    return matched[: max(1, int(max_groups or 10))]


def parse_member_count(text: str) -> int:
    """Parse strings like '15K thành viên', '8,2 nghìn thành viên', '5.1K members', '12.000 thành viên'."""
    text = (text or "").lower()
    m_k = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:k|nghìn|nghin)", text)
    if m_k:
        val_str = m_k.group(1).replace(",", ".")
        try:
            return int(float(val_str) * 1000)
        except ValueError:
            pass
    m_full = re.search(r"(\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?:thành viên|thanh vien|members|người|nguoi)", text)
    if m_full:
        val_str = re.sub(r"[.,]", "", m_full.group(1))
        try:
            return int(val_str)
        except ValueError:
            pass
    return 0


def search_facebook_groups(
    query: str = "kinh doanh tại nhật",
    min_members: int = 5000,
    limit: int = 10,
    state_path: Path | None = None,
    debug: bool = False,
) -> list[dict]:
    """Search Facebook for groups matching query and filter by minimum member count (e.g. >= 5000)."""
    state_path = Path(state_path or Path(__file__).parent / "fb-state.json")
    if not state_path.exists():
        raise RuntimeError(f"Missing login state {state_path}. Run first: python browser_worker.py --login")

    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()

        # Step 1: Go to FB home and ensure logged in
        try:
            page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        # Handle one-click profile resume if shown
        profile_btn = page.locator('div[role="button"][aria-label*="Tiếp tục"]').first
        if profile_btn.count() > 0:
            profile_btn.click()
            page.wait_for_timeout(4000)

        # Step 2: Use Facebook search input
        search_input = page.locator("input[placeholder*='Tìm kiếm'], input[placeholder*='Search Facebook'], input[placeholder*='Search'], input[aria-label*='Tìm kiếm'], input[type='search']").first
        if search_input.count() > 0:
            _human_type(search_input, query)
            page.wait_for_timeout(random.randint(600, 1100))
            search_input.press("Enter")
            page.wait_for_timeout(random.randint(4500, 6500))
        else:
            # Fallback to direct URL if search input not found
            encoded = quote(query)
            try:
                page.goto(f"https://www.facebook.com/search/groups/?q={encoded}", wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(5000)

        # Step 3: Switch to 'Nhóm' / 'Groups' tab if not already on it
        if "search/groups" not in page.url:
            group_tab = page.locator("a[href*='search/groups'], span:has-text('Nhóm'), span:has-text('Groups'), div[role='tab']:has-text('Nhóm')").first
            if group_tab.count() > 0:
                group_tab.click()
                page.wait_for_timeout(5000)

        # Step 4: Scroll down to load search result cards like a human
        for _ in range(4):
            _human_scroll_and_read(page, total_pixels=1400, min_read_sec=4.0, max_read_sec=7.0)

        # Step 5: Extract group links and container texts
        links_data = page.evaluate("""() => {
            const results = [];
            const links = Array.from(document.querySelectorAll("a[href*='/groups/']"));
            for (const a of links) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/\\/groups\\/([^\\/?#]+)/);
                if (!m) continue;
                const gid = m[1];
                if (['feed', 'joins', 'discover', 'categories'].includes(gid.toLowerCase())) continue;
                
                let container = a.closest("div[role='feed'] > div") || a.closest("div[role='article']") || a.parentElement?.parentElement?.parentElement;
                const cardText = container ? container.innerText : a.innerText;
                results.push({
                    href: href,
                    gid: gid,
                    linkText: (a.innerText || '').trim(),
                    cardText: cardText || ''
                });
            }
            return results;
        }""")
        browser.close()

    discovered: dict[str, dict] = {}
    for item in links_data:
        gid = item["gid"]
        if gid.lower() in _RESERVED_GROUP_SLUGS or re.search(r"/(?:posts|permalink|user|members|about|media|files)", item["href"]):
            continue
        card_text = item["cardText"]
        link_text = item["linkText"].strip()
        count = parse_member_count(card_text)
        name = _clean_group_name(link_text, gid)
        if not name or len(name) < 3 or "chưa đọc" in name.lower():
            continue

        if gid not in discovered:
            discovered[gid] = {
                "id": gid,
                "name": name,
                "url": f"https://www.facebook.com/groups/{gid}/",
                "member_count": count,
                "member_text": f"{count:,} thành viên" if count else "",
                "privacy": "Công khai" if "công khai" in card_text.lower() else "Riêng tư",
            }
        else:
            if count > discovered[gid]["member_count"]:
                discovered[gid]["member_count"] = count
                discovered[gid]["member_text"] = f"{count:,} thành viên"
            if len(name) > len(discovered[gid]["name"]):
                discovered[gid]["name"] = name

    # Filter >= min_members and sort descending by member count
    qualifying = [g for g in discovered.values() if g["member_count"] >= min_members]
    qualifying.sort(key=lambda x: x["member_count"], reverse=True)

    result = qualifying[:limit]
    return result


def _post_datetime(article) -> str:
    """Best-effort: read a <time datetime=...> inside a post, else ''."""
    try:
        t = article.locator("time").first
        if t.count():
            return t.get_attribute("datetime") or ""
    except Exception:
        pass
    return ""


def _extract_post_link_from_hrefs(hrefs: list[str], group_id: str) -> tuple[str, str]:
    """Extract (canonical_post_url, post_id) from a list of hrefs inside a post container.

    Supports all Facebook Group post link formats:
    1. /groups/<gid>/posts/<post_id> or /groups/<gid>/permalink/<post_id> (ignoring ?comment_id= links)
    2. ?multi_permalinks=<post_id>
    3. Photo/album posts inside a group: set=gm.<post_id> or set=pcb.<post_id>
    4. /story.php?story_fbid=<post_id>
    """
    # Pass 1: Direct permalink / posts URL without comment_id
    for raw in hrefs:
        if not raw or "comment_id=" in raw or "reply_comment_id=" in raw:
            continue
        m = re.search(r"/(?:permalink|posts)/(\d+)", raw)
        if m:
            pid = m.group(1)
            return f"https://www.facebook.com/groups/{group_id}/posts/{pid}/", pid

    # Pass 2: Group photo / multi-photo post links (`set=gm.<post_id>` or `set=pcb.<post_id>`)
    for raw in hrefs:
        if not raw or "comment_id=" in raw:
            continue
        m = re.search(r"[?&]set=(?:gm|pcb)\.(\d+)", raw)
        if m:
            pid = m.group(1)
            return f"https://www.facebook.com/groups/{group_id}/posts/{pid}/", pid
        m_pcb = re.search(r"/pcb\.(\d+)/", raw)
        if m_pcb:
            pid = m_pcb.group(1)
            return f"https://www.facebook.com/groups/{group_id}/posts/{pid}/", pid

    # Pass 3: multi_permalinks or story_fbid
    for raw in hrefs:
        if not raw or "comment_id=" in raw:
            continue
        m = re.search(r"(?:multi_permalinks|story_fbid)=(\d+)", raw)
        if m:
            pid = m.group(1)
            return f"https://www.facebook.com/groups/{group_id}/posts/{pid}/", pid

    # Pass 4: Fallback if only a comment link inside the post carries the post ID
    for raw in hrefs:
        if not raw:
            continue
        m = re.search(r"/(?:permalink|posts)/(\d+)", raw)
        if m:
            pid = m.group(1)
            return f"https://www.facebook.com/groups/{group_id}/posts/{pid}/", pid

    return "", ""


def _post_link(article, group_id: str = "") -> str:
    """Best-effort: find a real post link inside an article."""
    try:
        hrefs = article.locator("a[href]").evaluate_all(
            "els => els.map(e => e.getAttribute('href') || '')"
        )
    except Exception:
        hrefs = []
    link, _ = _extract_post_link_from_hrefs(hrefs, group_id or "group")
    return link


def _clean_profile_url(href: str, group_id: str) -> tuple[str, str]:
    """Turn a raw Facebook author link into (direct_author_url, group_member_url)."""
    if not href:
        return "", ""
    if href.startswith("/"):
        href = "https://www.facebook.com" + href

    # Pattern 1: /groups/<gid>/user/<uid>/
    m_user = re.search(r"/groups/[^/]+/user/(\d+)", href)
    if m_user:
        uid = m_user.group(1)
        group_member_url = f"https://www.facebook.com/groups/{group_id}/user/{uid}/"
        author_url = f"https://www.facebook.com/profile.php?id={uid}"
        return author_url, group_member_url

    # Pattern 2: profile.php?id=<uid>
    parsed = urlparse(href)
    if parsed.path.rstrip("/") == "/profile.php":
        qs = parse_qs(parsed.query)
        uid = (qs.get("id") or [""])[0]
        if uid:
            return f"https://www.facebook.com/profile.php?id={uid}", ""

    # Pattern 3: /<username>
    clean = href.split("?")[0].rstrip("/")
    return clean, ""


def _post_author_from_links(links: list[dict], group_id: str) -> dict:
    """Extract author_name, author_url, and group_member_url from link objects."""
    # 1. Prefer `/groups/.../user/<id>` link with visible author name text
    for item in links:
        href = item.get("href") or ""
        text = (item.get("text") or "").strip()
        if "/user/" in href and text and len(text) >= 2:
            if not any(
                skip in norm_vn(text)
                for skip in ("thich", "binh luan", "chia se", "xem them", "anh", "video", "tra loi")
            ):
                author_url, group_member_url = _clean_profile_url(href, group_id)
                return {
                    "author_name": text.splitlines()[0].strip()[:80],
                    "author_url": author_url,
                    "group_member_url": group_member_url,
                }

    # 2. Fallback: first profile link in header (not /groups/, /posts/, /permalink/, /hashtag/, /photo/)
    for item in links:
        href = item.get("href") or ""
        text = (item.get("text") or "").strip()
        if not href or not text or len(text) < 2:
            continue
        if any(
            bad in href
            for bad in ("/groups/", "/posts/", "/permalink/", "/hashtag/", "/photo", "/videos/", "/watch/", "comment_id=")
        ):
            continue
        author_url, group_member_url = _clean_profile_url(href, group_id)
        if author_url:
            return {
                "author_name": text.splitlines()[0].strip()[:80],
                "author_url": author_url,
                "group_member_url": group_member_url,
            }

    return {"author_name": "", "author_url": "", "group_member_url": ""}


def _check_fb_session(page, context) -> None:
    """Verify that the browser session is authenticated on Facebook.

    If Facebook has logged out, redirected to checkpoint/login, or is showing
    login inputs, IMMEDIATELY raises RuntimeError to halt scanning and alert the user.
    """
    # 1. Check URL
    url = (page.url or "").lower()
    if any(k in url for k in ("login", "checkpoint", "recover", "help/contact", "two_factor")):
        raise RuntimeError(
            "🚨 BỊ ĐĂNG XUẤT (LOGOUT): Facebook đã chuyển hướng sang trang đăng nhập/xác minh danh tính. "
            "Vui lòng bấm 'Đăng nhập FB' trên thanh công cụ để đăng nhập lại!"
        )

    # 2. Check live cookies
    try:
        cookies = context.cookies()
        has_c_user = any(c.get("name") == "c_user" and c.get("value") for c in cookies)
        if not has_c_user:
            raise RuntimeError(
                "🚨 BỊ ĐĂNG XUẤT (LOGOUT): Phiên đăng nhập Facebook đã hết hạn (không còn cookie c_user). "
                "Vui lòng bấm 'Đăng nhập FB' trên thanh công cụ để đăng nhập lại!"
            )
    except RuntimeError:
        raise
    except Exception:
        pass

    # 3. Check DOM markers (Login form / 'Đăng nhập / Bạn quên tài khoản ư?')
    try:
        has_login_input = page.locator('input[name="email"], input[name="pass"]').count() > 0
        if has_login_input:
            raise RuntimeError(
                "🚨 BỊ ĐĂNG XUẤT (LOGOUT): Facebook hiển thị khung đăng nhập (Tài khoản đã bị thoát). "
                "Vui lòng bấm 'Đăng nhập FB' trên thanh công cụ để đăng nhập lại!"
            )
        body_sample = (page.locator("body").inner_text(timeout=1000) or "").lower()[:2000]
        if "bạn quên tài khoản ư?" in body_sample or "log in to facebook" in body_sample:
            raise RuntimeError(
                "🚨 BỊ ĐĂNG XUẤT (LOGOUT): Facebook yêu cầu đăng nhập để xem nội dung. "
                "Vui lòng bấm 'Đăng nhập FB' trên thanh công cụ để đăng nhập lại!"
            )
    except RuntimeError:
        raise
    except Exception:
        pass


def _human_mouse_move(page, target_x: int | None = None, target_y: int | None = None) -> None:
    """Move the mouse smoothly across the screen simulating natural hand movement."""
    try:
        if target_x is None:
            target_x = random.randint(280, 820)
        if target_y is None:
            target_y = random.randint(220, 680)
        steps = random.randint(6, 14)
        page.mouse.move(target_x, target_y, steps=steps)
    except Exception:
        pass


def _human_type(locator, text: str) -> None:
    """Type text character-by-character with realistic human keystroke intervals and pauses."""
    try:
        locator.click()
        page = locator.page
        page.wait_for_timeout(random.randint(250, 480))
        for idx, char in enumerate(text):
            delay = random.randint(75, 195)
            locator.press_sequentially(char, delay=delay)
            # Add thinking pause on space or every 4-6 chars
            if char in (" ", "-", ",") or (idx > 0 and idx % random.randint(4, 7) == 0):
                page.wait_for_timeout(random.randint(180, 420))
    except Exception:
        try:
            locator.fill(text)
        except Exception:
            pass


def _human_scroll_and_read(
    page,
    total_pixels: int = 1600,
    min_read_sec: float = 5.0,
    max_read_sec: float = 10.0,
) -> None:
    """Simulate realistic human scrolling, cursor tracking, and reading pauses on Facebook feeds.

    1. Smooth incremental mouse wheel scrolling in 2-4 steps rather than sharp jumps.
    2. Tracking mouse movements following the scroll direction.
    3. 25% chance of a slight reverse scroll (50px - 140px) like rereading a paragraph.
    4. Deliberate reading pause: 5.0s to 10.0s (with occasional deeper pause up to 13s).
    5. Mid-pause cursor adjustments simulating active human presence.
    """
    steps = random.randint(2, 4)
    step_px = max(180, total_pixels // steps)
    for _ in range(steps):
        actual_px = int(step_px * random.uniform(0.85, 1.25))
        try:
            page.mouse.wheel(0, actual_px)
        except Exception:
            try:
                page.evaluate(f"window.scrollBy(0, {actual_px})")
            except Exception:
                pass
        # Natural cursor tracking while reading
        _human_mouse_move(page)
        page.wait_for_timeout(random.randint(320, 680))

    # 25% chance of rereading: small scroll up and mouse hover
    if random.random() < 0.25:
        page.wait_for_timeout(random.randint(350, 750))
        try:
            page.mouse.wheel(0, -random.randint(50, 130))
        except Exception:
            pass
        _human_mouse_move(page)
        page.wait_for_timeout(random.randint(300, 600))

    # Reading pause: 5.0s to 10.0s (15% chance of reading longer up to 13.5s)
    if random.random() < 0.15:
        read_time_ms = int(random.uniform(10.0, 13.5) * 1000)
    else:
        read_time_ms = int(random.uniform(min_read_sec, max_read_sec) * 1000)

    # Halfway through reading, make a subtle cursor adjustment
    halfway = read_time_ms // 2
    page.wait_for_timeout(halfway)
    _human_mouse_move(page)
    page.wait_for_timeout(read_time_ms - halfway)


def _expand_see_more(container) -> None:
    """Click 'Xem thêm' / 'See more' / 'さらに表示' inside a post container with realistic mouse approach."""
    for label in ("Xem thêm", "See more", "さらに表示"):
        try:
            btn = container.locator(f"div[role='button']:has-text('{label}')").first
            if btn.count() and btn.is_visible():
                try:
                    box = btn.bounding_box()
                    page = container.page
                    if box and hasattr(page, "mouse"):
                        page.mouse.move(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                            steps=random.randint(4, 8)
                        )
                        page.wait_for_timeout(random.randint(150, 320))
                except Exception:
                    pass
                btn.click(timeout=600)
                return
        except Exception:
            continue


def _is_comment_element(container) -> bool:
    """True when a `div[role='article']` is actually a comment/reply rather than a feed post."""
    try:
        aria = (container.get_attribute("aria-label") or "").lower()
        if any(
            marker in norm_vn(aria)
            for marker in ("binh luan duoi ten", "binh luan cua", "phan hoi cua", "comment by", "reply by")
        ):
            return True
    except Exception:
        pass
    return False


def _clean_post_text(raw_text: str) -> str:
    """Strip noisy comment threads from the bottom of an extracted post card."""
    text = (raw_text or "").strip()
    # Cut off at the comment section footer if present
    lines = text.splitlines()
    kept_lines: list[str] = []
    for ln in lines:
        low = norm_vn(ln.strip())
        if low in ("viet binh luan...", "viet binh luan cong khai...", "write a comment...", "write a public comment..."):
            break
        if low.startswith(("xem them binh luan", "view more comments", "tat ca binh luan", "phu hop nhat")):
            break
        kept_lines.append(ln)
    return "\n".join(kept_lines).strip()


def _dump_debug(page, containers, count: int, group_id: str = "") -> None:
    """Save what the page actually contains, so a failed run can be diagnosed."""
    sample_links: list[str] = []
    sample_texts: list[str] = []
    try:
        for i in range(min(count, 10)):
            try:
                hrefs = containers.nth(i).locator("a[href]").evaluate_all(
                    "els => els.slice(0, 8).map(e => e.getAttribute('href'))"
                )
                sample_links.extend(h for h in hrefs if h)
            except Exception:
                pass
            try:
                sample_texts.append(containers.nth(i).inner_text(timeout=1000)[:300])
            except Exception:
                pass
    except Exception:
        pass
    data = {
        "group_id": group_id,
        "url": page.url,
        "container_count": count,
        "sample_links": sample_links[:40],
        "sample_texts": sample_texts[:8],
    }
    Path(__file__).parent.joinpath("debug.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[debug] wrote debug.json: {count} containers at {page.url}", file=sys.stderr)


_DOM_EXTRACT_CARDS_JS = """() => {
    const feed = document.querySelector("div[role='feed']") || document.querySelector("div[role='main']") || document.body;
    const candidates = Array.from(feed.querySelectorAll("div[role='article'], div[aria-posinset], div[data-ad-comet-preview='message'], div[data-ad-preview='message']"));
    const units = new Set();
    for (const el of candidates) {
        let card = el;
        if (!card.getAttribute('role') && !card.hasAttribute('aria-posinset')) {
            for (let step = 0; step < 8 && card.parentElement && card.parentElement !== feed; step++) {
                card = card.parentElement;
                if (card.querySelector("a[href*='/user/'], h2 a[href], h3 a[href], h4 a[href]")) {
                    break;
                }
            }
        }
        units.add(card);
    }
    for (const div of Array.from(feed.querySelectorAll('div'))) {
        if (div.children.length >= 3 && div.children.length <= 50) {
            let postLikeChildren = 0;
            for (const ch of div.children) {
                if (ch.querySelector && ch.querySelector("a[href*='/user/'], a[href*='set=gm.'], a[href*='/posts/']")) {
                    postLikeChildren++;
                }
            }
            if (postLikeChildren >= 2) {
                for (const ch of div.children) units.add(ch);
            }
        }
    }
    const results = [];
    for (const card of units) {
        const aria = (card.getAttribute('aria-label') || '').toLowerCase();
        if (aria.includes('bình luận') || aria.includes('comment') || aria.includes('phản hồi') || aria.includes('reply')) {
            continue;
        }
        const userLinks = Array.from(card.querySelectorAll("a[href*='/user/']")).map(a => (a.getAttribute('href') || '').split('?')[0]);
        const uniqueUsers = new Set(userLinks.filter(Boolean));
        if (uniqueUsers.size > 3) {
            continue;
        }
        const links = Array.from(card.querySelectorAll('a[href]')).slice(0, 35).map(a => ({
            href: a.getAttribute('href') || '',
            text: (a.innerText || a.getAttribute('aria-label') || '').trim()
        }));
        let timeText = '';
        const abbrEl = card.querySelector('abbr, time');
        if (abbrEl) {
            timeText = (abbrEl.getAttribute('datetime') || abbrEl.innerText || abbrEl.getAttribute('title') || abbrEl.getAttribute('aria-label') || '').trim();
        }
        if (!timeText) {
            const timeLinks = Array.from(card.querySelectorAll("a[href*='/posts/'], a[href*='/permalink/'], a[href*='set=gm.'], a[href*='multi_permalinks']"));
            for (const tl of timeLinks) {
                const t = (tl.innerText || tl.getAttribute('aria-label') || '').trim();
                if (t && (t.match(/\\d+\\s*(?:giờ|phút|ngày|tháng|h|m|d|min|hr|yr|year|month)/i) || t.match(/hôm qua|yesterday|vừa xong|just now/i))) {
                    timeText = t;
                    break;
                }
            }
        }
        results.push({
            text: (card.innerText || '').trim(),
            links: links,
            datetime: timeText || ''
        });
    }
    return results;
}"""

DEFAULT_GENERAL_SEARCH_QUERIES = [
    "tuyển",
    "quán",
    "nail",
    "thực phẩm",
    "thanh lý",
    "xe ghép",
]

DEFAULT_BEAUTY_SEARCH_QUERIES = [
    "tuyển thợ",
    "nối mi",
    "mẫu",
    "spa",
    "tiệm",
]


def fetch_group_posts(
    group_id: str,
    keyword: str,
    limit: int,
    date_from,
    date_to,
    state_path: Path,
    debug: bool = False,
) -> list[dict]:
    """Open the group feed in a logged-in browser and extract posts + author info."""
    if not state_path.exists():
        raise RuntimeError(
            f"Missing login state {state_path}. Run first: python browser_worker.py --login"
        )
    # Sort by CHRONOLOGICAL (NEWEST first) so we always get fresh Baito posts
    url = f"https://www.facebook.com/groups/{group_id}/?sorting_setting=CHRONOLOGICAL"
    posts: list[dict] = []
    seen_pids: set[str] = set()
    seen_text_hashes: set[str] = set()
    saw_any_feed_item = False

    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            # Even if domcontentloaded times out because of background scripts,
            # check if the page rendered feed content before giving up.
            pass
        page.wait_for_timeout(random.randint(3500, 5500))
        try:
            _check_fb_session(page, context)
        except RuntimeError:
            browser.close()
            raise

        max_scrolls = max(6, limit // 2)
        for scroll_idx in range(max_scrolls):
            if len(posts) >= limit:
                break
            # Human scrolling & reading pause (5s - 10s)
            _human_scroll_and_read(page, total_pixels=1800, min_read_sec=5.0, max_read_sec=10.0)

            for label in ("Xem thêm", "See more", "さらに表示"):
                try:
                    btns = page.locator(f"div[role='button']:has-text('{label}')")
                    for b_idx in range(min(btns.count(), 6)):
                        try:
                            btns.nth(b_idx).click(timeout=350)
                        except Exception:
                            pass
                except Exception:
                    pass

            # Extract individual post cards directly from the live DOM in JS,
            # avoiding giant feed wrappers that contain multiple posts.
            raw_cards = page.evaluate(_DOM_EXTRACT_CARDS_JS)

            for card in raw_cards or []:
                if len(posts) >= limit:
                    break
                raw_text = card.get("text") or ""
                link_objs = card.get("links") or []
                hrefs = [obj.get("href") or "" for obj in link_objs if obj.get("href")]
                link, post_id = _extract_post_link_from_hrefs(hrefs, str(group_id))
                author = _post_author_from_links(link_objs, str(group_id))

                if not link and not author["group_member_url"] and not author["author_url"]:
                    continue

                author_name = author["author_name"]
                cleaned_body = _clean_post_text(raw_text)
                text = clean_fb_noise(cleaned_body, author_name=author_name)
                if not text or len(text) < 20:
                    continue

                if not author_name:
                    # Only treat the first line as a name when it looks like one
                    # (short, no sentence). A long headline such as
                    # "Ghé JAPAN TRAVEL lấy vé về thăm gia đình ạ" is post content
                    # and must stay in the text so classification can see it.
                    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
                    if 2 <= len(first_line) <= 40 and len(first_line.split()) <= 5:
                        author_name = first_line
                        text = clean_fb_noise(text, author_name=author_name)

                saw_any_feed_item = True

                text_sig = norm_vn(text[:180])
                if post_id and post_id in seen_pids:
                    continue
                if text_sig in seen_text_hashes:
                    continue
                if post_id:
                    seen_pids.add(post_id)
                seen_text_hashes.add(text_sig)

                if not link:
                    link = author["group_member_url"] or f"https://www.facebook.com/groups/{group_id}/"
                if not post_id:
                    post_id = f"hash_{abs(hash(text_sig)) % 10_000_000_000}"

                created = card.get("datetime") or ""
                if not in_date_range(created, date_from, date_to):
                    continue
                if keyword and not matches_any_keyword(text, keyword):
                    continue

                posts.append({
                    "post_id": post_id,
                    "post_url": link,
                    "author_name": author_name,
                    "author_url": author["author_url"],
                    "group_member_url": author["group_member_url"],
                    "created_at": created,
                    "content": text[:4000],
                    "react_count": 0,
                    "comment_count": 0,
                    "extracted": {},
                })
        browser.close()

    if not posts and not saw_any_feed_item:
        raise RuntimeError(
            "No posts could be read from the group page. Facebook may have "
            "changed its layout, asked for a login check, or the group is "
            "not viewable with this account. Try --login again, or run with "
            "--debug and send debug.json to check what the page actually shows."
        )
    return posts


def fetch_group_posts_deep_search(
    group_id: str,
    queries: list[str],
    limit_per_query: int,
    date_from: str | None,
    date_to: str | None,
    state_path: Path,
    debug: bool = False,
) -> list[dict]:
    """Search multiple industry keywords inside a group to gather historical posts (past 1 year)."""
    if not state_path.exists():
        raise RuntimeError(
            f"Missing login state {state_path}. Run first: python browser_worker.py --login"
        )
    posts: list[dict] = []
    seen_pids: set[str] = set()
    seen_text_hashes: set[str] = set()

    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()

        for q_idx, query in enumerate(queries):
            url = f"https://www.facebook.com/groups/{group_id}/search/?q={quote(query)}"
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=40000)
            except Exception:
                pass
            page.wait_for_timeout(random.randint(3500, 5000))
            try:
                _check_fb_session(page, context)
            except RuntimeError:
                browser.close()
                raise

            max_scrolls = max(3, limit_per_query // 3)
            for _ in range(max_scrolls):
                # Human scrolling & reading pause (5s - 10s)
                _human_scroll_and_read(page, total_pixels=1600, min_read_sec=5.0, max_read_sec=10.0)

                for label in ("Xem thêm", "See more", "さらに表示"):
                    try:
                        btns = page.locator(f"div[role='button']:has-text('{label}')")
                        for b_idx in range(min(btns.count(), 4)):
                            try:
                                btns.nth(b_idx).click(timeout=300)
                            except Exception:
                                pass
                    except Exception:
                        pass

            raw_cards = page.evaluate(_DOM_EXTRACT_CARDS_JS)
            for card in raw_cards or []:
                raw_text = card.get("text") or ""
                link_objs = card.get("links") or []
                hrefs = [obj.get("href") or "" for obj in link_objs if obj.get("href")]
                link, post_id = _extract_post_link_from_hrefs(hrefs, str(group_id))
                author = _post_author_from_links(link_objs, str(group_id))

                if not link and not author["group_member_url"] and not author["author_url"]:
                    continue

                author_name = author["author_name"]
                cleaned_body = _clean_post_text(raw_text)
                text = clean_fb_noise(cleaned_body, author_name=author_name)
                if not text or len(text) < 20:
                    continue

                if not author_name:
                    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
                    if 2 <= len(first_line) <= 40 and len(first_line.split()) <= 5:
                        author_name = first_line
                        text = clean_fb_noise(text, author_name=author_name)

                text_sig = norm_vn(text[:180])
                if post_id and post_id in seen_pids:
                    continue
                if text_sig in seen_text_hashes:
                    continue
                if post_id:
                    seen_pids.add(post_id)
                seen_text_hashes.add(text_sig)

                if not link:
                    link = author["group_member_url"] or f"https://www.facebook.com/groups/{group_id}/"
                if not post_id:
                    post_id = f"hash_{abs(hash(text_sig)) % 10_000_000_000}"

                created = card.get("datetime") or ""
                if not in_date_range(created, date_from, date_to):
                    continue

                posts.append({
                    "post_id": post_id,
                    "post_url": link,
                    "author_name": author_name,
                    "author_url": author["author_url"],
                    "group_member_url": author["group_member_url"],
                    "created_at": created,
                    "content": text[:4000],
                    "react_count": 0,
                    "comment_count": 0,
                    "extracted": {},
                })

            if q_idx < len(queries) - 1:
                inter_delay = int(random.uniform(7.0, 12.0) * 1000)
                _human_mouse_move(page)
                page.wait_for_timeout(inter_delay)

        browser.close()

    return posts


def load_seen(path: Path) -> dict:
    """Seen post ids per group, from seen.json. Missing/corrupt file -> {}."""
    try:
        data = load_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_seen(path: Path, seen: dict) -> None:
    save_json(path, seen)


def _update_leads_files(root: Path, new_posts: list[dict]) -> list[dict]:
    """Merge newly matched small business owner posts into leads.json and leads.csv."""
    leads_json_path = root / "leads.json"
    leads_csv_path = root / "leads.csv"
    existing: list[dict] = []
    if leads_json_path.exists():
        try:
            loaded = load_json(leads_json_path)
            if isinstance(loaded, list):
                existing = loaded
            elif isinstance(loaded, dict) and isinstance(loaded.get("leads"), list):
                existing = loaded["leads"]
        except Exception:
            existing = []
    merged = build_leads_list(new_posts, existing_leads=existing)
    if merged:
        try:
            save_json(
                leads_json_path,
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "total_leads": len(merged),
                    "leads": merged,
                },
            )
            save_leads_csv(leads_csv_path, merged)
            try:
                from dashboard import generate as generate_dashboard
                generate_dashboard(root, save_leads=False)
            except Exception:
                pass
        except Exception:
            pass
    return merged


def _scan_one_group(
    task: dict,
    group_id: str,
    state_path: Path,
    seen: dict,
    debug: bool = False,
    group_name: str = "",
) -> tuple[list[dict], int, str | None, dict]:
    """Scan one group: fetch, extract, filter, drop already-seen posts.

    Returns (kept_posts, skipped_seen_count, error_or_None, stats_dict).
    """
    limit = max(1, min(int(task.get("limit", 15) or 15), 50))
    wanted = task.get("fields_to_extract", []) or [
        "phone",
        "contact",
        "business_type",
        "shop_name",
        "scale",
        "location",
        "station",
        "wage",
        "roles_needed",
        "needs",
    ]
    wanted = list(dict.fromkeys([*wanted, "level", "time", "price", "wage", "location"]))
    keyword = (task.get("post_keyword") or "").strip()
    deep_cfg = task.get("deep_search") or {}
    is_deep = bool(task.get("deep") or (deep_cfg.get("enabled", False) if isinstance(deep_cfg, dict) else bool(deep_cfg)))
    date_from = task.get("date_from")
    if is_deep and not date_from:
        date_from = (date.today() - timedelta(days=365)).isoformat()

    try:
        if is_deep:
            g_ident = f"{group_id} {group_name}".lower()
            if any(k in g_ident for k in ("nail", "móng", " mi ", "spa", "makeup", "làm đẹp", "thẩm mỹ")):
                queries = (deep_cfg.get("search_queries") if isinstance(deep_cfg, dict) and deep_cfg.get("search_queries") else None) or DEFAULT_BEAUTY_SEARCH_QUERIES
            else:
                queries = (deep_cfg.get("search_queries") if isinstance(deep_cfg, dict) and deep_cfg.get("search_queries") else None) or DEFAULT_GENERAL_SEARCH_QUERIES

            per_query_limit = int(deep_cfg.get("posts_per_query", 15)) if isinstance(deep_cfg, dict) else 15
            raw = fetch_group_posts_deep_search(
                group_id,
                queries,
                limit_per_query=per_query_limit,
                date_from=date_from,
                date_to=task.get("date_to"),
                state_path=state_path,
                debug=debug,
            )
        else:
            raw = fetch_group_posts(
                group_id,
                keyword,
                limit,
                task.get("date_from"),
                task.get("date_to"),
                state_path,
                debug=debug,
            )
    except Exception as exc:
        err_msg = str(exc)
        if any(marker in err_msg for marker in ("BỊ ĐĂNG XUẤT", "LOGOUT", "checkpoint", "hết hạn")):
            raise
        return [], 0, err_msg, {"raw_posts_read": 0}

    breakdown: dict[str, int] = {
        "small_business_owner": 0,
        "job_seeker": 0,
        "agency_or_large": 0,
        "unknown": 0,
    }
    for post in raw:
        post["group_id"] = str(group_id)
        if group_name:
            post["group_name"] = group_name
        post["content"] = clean_fb_noise(post.get("content", ""), author_name=post.get("author_name", ""))
        post["extracted"] = extract_fields(
            post.get("content", ""),
            wanted,
            author_name=post.get("author_name", ""),
            group_name=group_name,
            group_id=str(group_id),
        )
        analysis = analyze_business_and_needs(
            post.get("content", ""),
            author_name=post.get("author_name", ""),
            group_name=group_name,
            group_id=str(group_id),
        )
        post["business_analysis"] = analysis
        cat = analysis.get("post_category", "unknown")
        breakdown[cat] = breakdown.get(cat, 0) + 1

    if debug:
        debug_path = Path(__file__).parent / "debug.json"
        existing_dbg: dict = {}
        if debug_path.exists():
            try:
                existing_dbg = load_json(debug_path)
                if not isinstance(existing_dbg, dict):
                    existing_dbg = {}
            except Exception:
                existing_dbg = {}
        by_grp = existing_dbg.get("groups_raw_posts") or {}
        by_grp[str(group_id)] = [
            {
                "post_id": p.get("post_id"),
                "author_name": p.get("author_name"),
                "author_url": p.get("author_url"),
                "category": (p.get("business_analysis") or {}).get("post_category"),
                "reason": (p.get("business_analysis") or {}).get("classification_reason"),
                "content_preview": (p.get("content") or "")[:300],
            }
            for p in raw
        ]
        existing_dbg["groups_raw_posts"] = by_grp
        save_json(debug_path, existing_dbg)

    kept = [post for post in raw if post_matches_filters(post, task)]
    skipped_seen = 0
    if task.get("only_new"):
        known = set(seen.get(str(group_id), []))
        fresh = []
        for post in kept:
            pid = post.get("post_id") or ""
            if pid and pid in known:
                skipped_seen += 1
                continue
            fresh.append(post)
        kept = fresh
        known.update(p.get("post_id") for p in kept if p.get("post_id"))
        seen[str(group_id)] = sorted(known)[-500:]

    stats = {
        "raw_posts_read": len(raw),
        "classification_breakdown": breakdown,
        "skipped_already_seen": skipped_seen,
    }
    return kept, skipped_seen, None, stats


def run_task(task: dict, state_path: Path, demo: bool, debug: bool = False) -> dict:
    root = Path(__file__).parent
    task_id = task.get("task_id", "")
    action = task.get("action", "")
    if demo:
        sample = load_json(root / "example-result.json")
        sample["task_id"] = task_id or sample.get("task_id", "TASK-DEMO")
        sample["status"] = "success"
        sample["summary"] = "Demo mode: replayed bundled example result, no browser was opened."
        if sample.get("posts"):
            sample["leads"] = _update_leads_files(root, sample["posts"])
        return sample
    if action != "search_group_posts":
        return {
            "task_id": task_id,
            "status": "error",
            "errors": ["Browser worker v1 only supports action search_group_posts."],
            "posts": [],
            "leads": [],
            "groups": [],
            "summary": "Task rejected: browser worker supports search_group_posts only.",
        }

    group_entries: list[dict] = []
    group_ids = task.get("group_ids") or []
    if isinstance(group_ids, str):
        group_ids = [group_ids]
    group_ids = [
        str(g).strip()
        for g in group_ids
        if str(g).strip() and str(g).strip() != "THAY_TEN_NHOM_O_DAY"
    ]
    if not group_ids:
        single = task.get("group_id") or task.get("group_name", "")
        if single and str(single).strip() != "THAY_TEN_NHOM_O_DAY":
            group_ids = [str(single).strip()]

    for gid in group_ids:
        group_entries.append({"id": gid, "name": "", "url": f"https://www.facebook.com/groups/{gid}/"})

    if task.get("discover_joined_groups") or not group_entries:
        try:
            discovered = discover_joined_groups(
                state_path=state_path,
                group_keywords=task.get("group_keywords"),
                max_groups=int(task.get("max_groups", 10) or 10),
                debug=debug,
            )
            existing_ids = {g["id"] for g in group_entries}
            for g in discovered:
                if g["id"] not in existing_ids:
                    group_entries.append(g)
                    existing_ids.add(g["id"])
        except Exception as exc:
            if not group_entries:
                return {
                    "task_id": task_id,
                    "status": "error",
                    "errors": [f"Could not discover joined groups: {exc}"],
                    "posts": [],
                    "leads": [],
                    "groups": [],
                    "summary": f"Task failed discovering joined groups: {exc}",
                }

    if not group_entries:
        return {
            "task_id": task_id,
            "status": "error",
            "errors": [
                "No matching joined Baito groups found (and no group_id/group_ids provided). "
                "Join Baito groups on Facebook first or set group_keywords: [] to scan all joined groups."
            ],
            "posts": [],
            "leads": [],
            "groups": [],
            "summary": "Task failed: no matching joined Baito groups found.",
        }

    raw_pause = task.get("group_pause_seconds", 35)
    pause = 35 if (raw_pause is None or raw_pause < 25) else max(25, min(int(raw_pause), 300))
    seen: dict = {}
    if task.get("only_new"):
        seen = load_seen(root / "seen.json")
    posts: list[dict] = []
    groups_out: list[dict] = []
    errors: list[str] = []
    skipped_total = 0
    raw_total = 0

    for i, g_entry in enumerate(group_entries):
        gid = g_entry["id"]
        gname = g_entry.get("name", "")
        if i > 0 and pause:
            pause_time = _jittered(pause, 35)
            print(f"[human-pause] Nghỉ tự nhiên {pause_time:.1f}s giữa các nhóm để tránh nghi ngờ...")
            time.sleep(pause_time)
        try:
            kept, skipped, error, stats = _scan_one_group(
                task, gid, state_path, seen, debug=debug, group_name=gname
            )
        except Exception as exc:
            err_text = str(exc)
            print(f"🚨 [DỪNG KHẨN CẤP] {err_text}", file=sys.stderr)
            errors.append(err_text)
            groups_out.append({
                "id": str(gid),
                "name": gname,
                "url": f"https://www.facebook.com/groups/{gid}/",
                "raw_posts_read": 0,
                "classification_breakdown": {},
                "found": 0,
                "error": err_text,
            })
            # Break immediately on fatal session error
            break

        skipped_total += skipped
        raw_total += stats.get("raw_posts_read", 0)
        posts.extend(kept)
        groups_out.append({
            "id": str(gid),
            "name": gname,
            "url": f"https://www.facebook.com/groups/{gid}/",
            "raw_posts_read": stats.get("raw_posts_read", 0),
            "classification_breakdown": stats.get("classification_breakdown", {}),
            "found": len(kept),
            "error": error or "",
        })
        if error:
            errors.append(f"Group {gid}: {error}")

    if task.get("only_new"):
        save_seen(root / "seen.json", seen)

    run_leads = build_leads_list(posts)
    if posts:
        _update_leads_files(root, posts)

    ok_count = sum(1 for g in groups_out if not g["error"])
    new_note = f" ({skipped_total} already seen skipped)" if skipped_total else ""
    first_g = group_entries[0]
    if ok_count == 0:
        return {
            "task_id": task_id,
            "status": "error",
            "errors": errors,
            "group": {
                "id": str(first_g["id"]),
                "name": first_g.get("name", ""),
                "url": f"https://www.facebook.com/groups/{first_g['id']}/",
            },
            "posts": [],
            "leads": [],
            "groups": groups_out,
            "summary": f"Task failed in all {len(group_entries)} group(s): {errors[0] if errors else 'unknown error'}",
        }
    return {
        "task_id": task_id,
        "status": "success",
        "errors": errors,
        "group": {
            "id": str(first_g["id"]),
            "name": first_g.get("name", ""),
            "url": f"https://www.facebook.com/groups/{first_g['id']}/",
        },
        "posts": posts,
        "leads": run_leads,
        "groups": groups_out,
        "summary": (
            f"Read {raw_total} raw post(s) -> Matched {len(posts)} post(s) / "
            f"{len(run_leads)} small-business owner(s) in {ok_count}/{len(group_entries)} Baito group(s){new_note}."
        ),
    }


def watch_loop(
    task: dict,
    state_path: Path,
    out_path: Path,
    root: Path,
    demo: bool = False,
    debug: bool = False,
    sleep_fn=time.sleep,
    run_fn=None,
    max_cycles=None,
) -> int:
    """Repeat the task until Ctrl+C or a stop condition."""
    if run_fn is None:
        run_fn = run_task
    interval = max(5, int(task.get("watch_interval_minutes", 30) or 30))
    raw_jitter = task.get("watch_interval_jitter_percent", 20)
    jitter_pct = 20 if raw_jitter is None else max(0, min(float(raw_jitter), 50))
    print(f"Watching every ~{interval} minute(s) (+/-{jitter_pct:.0f}%). Press Ctrl+C to stop.")
    print('Tip: set "only_new": true in the task so each scan only reports new posts.')
    watch_log = root / "watch-new.jsonl"
    pid_path = root / "watch.pid"
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    consecutive_errors = 0
    scans_total = 0
    found_total = 0
    last_summary = ""
    stop_reason = ""
    try:
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            cycle += 1
            scans_total += 1
            try:
                result = run_fn(task, state_path, demo=demo, debug=debug)
            except Exception as exc:  # noqa: BLE001 - one bad scan must not kill the loop
                result = {
                    "task_id": task.get("task_id", ""),
                    "status": "error",
                    "errors": [f"Scan crashed: {exc}"],
                    "posts": [],
                    "leads": [],
                    "summary": f"Scan crashed: {exc}",
                }
            try:
                save_json(out_path, result)
            except Exception:
                pass
            new_posts = result.get("posts", []) or []
            found_total += len(new_posts)
            last_summary = result.get("summary", "")
            print(f"[{time.strftime('%H:%M:%S')}] {last_summary}")
            if new_posts:
                try:
                    with watch_log.open("a", encoding="utf-8") as f:
                        for post in new_posts:
                            f.write(
                                json.dumps(
                                    {
                                        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "task_id": result.get("task_id", ""),
                                        **post,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                    print(
                        f"  -> {len(new_posts)} new post(s) appended to watch-new.jsonl "
                        f"and updated in leads.json / leads.csv"
                    )
                except Exception as exc:
                    print(f"  !! could not append to watch-new.jsonl: {exc}")
            status = "running"
            if result.get("status") == "error":
                consecutive_errors += 1
                status = "error"
                errors = result.get("errors", []) or []
                if any(_is_session_error(e) for e in errors):
                    stop_reason = "session_expired"
                elif consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    stop_reason = "repeated_errors"
            else:
                consecutive_errors = 0
            wait_seconds = _jittered(interval * 60, jitter_pct)
            _write_status(
                root,
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "task_id": task.get("task_id", ""),
                    "status": "stopped" if stop_reason else status,
                    "stop_reason": stop_reason,
                    "last_summary": last_summary,
                    "consecutive_errors": consecutive_errors,
                    "scans_total": scans_total,
                    "found_total": found_total,
                    "next_scan_in_seconds": round(wait_seconds) if not stop_reason else None,
                },
            )
            if stop_reason:
                if stop_reason == "session_expired":
                    print("STOP: Facebook session expired (login/checkpoint redirect).")
                    print("Run again: python browser_worker.py --login  (then restart the watch)")
                else:
                    print(
                        f"STOP: {consecutive_errors} failed scans in a row. "
                        "Check the errors above / debug.json, fix the cause, then restart."
                    )
                return 1
            sleep_fn(wait_seconds)
    except KeyboardInterrupt:
        stop_reason = "user_stopped"
        print("\nWatch stopped.")
    finally:
        if stop_reason:
            _write_status(
                root,
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "task_id": task.get("task_id", ""),
                    "status": "stopped",
                    "stop_reason": stop_reason,
                    "last_summary": last_summary,
                    "consecutive_errors": consecutive_errors,
                    "scans_total": scans_total,
                    "found_total": found_total,
                    "next_scan_in_seconds": None,
                },
            )
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AGENT-V1 browser worker (Baito Japan Lead Finder)")
    parser.add_argument("--task", default="task-baito-nhom-da-tham-gia.json")
    parser.add_argument("--out", default="result.json")
    parser.add_argument("--login", action="store_true", help="Log in once and save fb-state.json")
    parser.add_argument("--login-web", action="store_true", help="Log in from the web control center")
    parser.add_argument(
        "--list-groups",
        action="store_true",
        help="List Baito groups that your logged-in Facebook account has joined",
    )
    parser.add_argument("--demo", action="store_true", help="Replay bundled example, no browser")
    parser.add_argument("--debug", action="store_true", help="Write debug.json with what the page actually shows")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Rebuild dashboard.html and open it in the default browser",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Repeat the task every watch_interval_minutes (from the task file, default 30, min 5) until Ctrl+C",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Deep search: query industry keywords across the past 1 year in target groups",
    )
    parser.add_argument(
        "--search-groups",
        metavar="QUERY",
        help="Search Facebook for groups matching QUERY and filter by --min-members (default: 5000)",
    )
    parser.add_argument(
        "--min-members",
        type=int,
        default=5000,
        help="Minimum member count for discovered groups (default: 5000)",
    )
    parser.add_argument(
        "--limit-groups",
        type=int,
        default=10,
        help="Maximum number of discovered groups to return (default: 10)",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).parent
    state_path = root / "fb-state.json"
    if args.dashboard:
        try:
            from dashboard import generate as generate_dashboard
            import webbrowser
            out_file = generate_dashboard(root, save_leads=True)
            print(f"Generated dashboard: {out_file}")
            webbrowser.open(out_file.resolve().as_uri())
        except Exception as exc:
            print(f"Error generating dashboard: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.login or args.login_web:
        try:
            login(state_path, web_mode=args.login_web)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.list_groups:
        try:
            groups = discover_joined_groups(state_path, group_keywords=None, max_groups=50, debug=args.debug)
            print(json.dumps({"matched_baito_groups": groups}, ensure_ascii=False, indent=2))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.search_groups:
        try:
            print(f"Đang tìm kiếm các nhóm FB liên quan đến '{args.search_groups}' có trên {args.min_members:,} thành viên...")
            groups = search_facebook_groups(
                query=args.search_groups,
                min_members=args.min_members,
                limit=args.limit_groups,
                state_path=state_path,
                debug=args.debug,
            )
            out_disc = {
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "query": args.search_groups,
                "min_members": args.min_members,
                "total_found": len(groups),
                "groups": groups,
            }
            disc_path = root / "discovered_groups.json"
            save_json(disc_path, out_disc)
            print(f"Đã tìm thấy {len(groups)} nhóm đạt tiêu chuẩn >= {args.min_members:,} thành viên!")
            for idx, g in enumerate(groups, 1):
                print(f"  #{idx}: {g['name']} ({g['member_count']:,} mem) -> {g['url']}")
            print(f"Đã lưu danh sách nhóm vào {disc_path}")
            print(json.dumps(out_disc, ensure_ascii=False, indent=2))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    task_path = Path(args.task)
    if not task_path.exists():
        print(f"Task file not found: {task_path}", file=sys.stderr)
        return 2
    task = load_json(task_path)
    if args.deep:
        task["deep"] = True
    if not args.watch:
        result = run_task(task, state_path, demo=args.demo, debug=args.debug)
        out_path = Path(args.out)
        if out_path.exists():
            try:
                prev = load_json(out_path)
                if isinstance(prev, dict):
                    prev_groups = {str(g["id"]): g for g in prev.get("groups", []) if g.get("id")}
                    for g in result.get("groups", []):
                        if g.get("id"):
                            prev_groups[str(g["id"])] = g
                    result["groups"] = list(prev_groups.values())

                    prev_posts = {str(p.get("post_id")): p for p in prev.get("posts", []) if p.get("post_id")}
                    for p in result.get("posts", []):
                        if p.get("post_id"):
                            prev_posts[str(p["post_id"])] = p
                    result["posts"] = list(prev_posts.values())
            except Exception:
                pass
        save_json(out_path, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    return watch_loop(task, state_path, Path(args.out), root, demo=args.demo, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
