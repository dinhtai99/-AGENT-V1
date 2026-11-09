#!/usr/bin/env python3
"""
AGENT-V1 — Browser worker (experimental fallback).

Same task/result contract as worker.py, but it reads Facebook through a
real logged-in browser instead of the Graph API. Use it only when the
Graph API refuses group access.

How it works:
  1. Login once and save the session (headed browser, you log in by hand):
         python browser_worker.py --login
     This saves fb-state.json next to this file. Never share or commit it.
     .gitignore already excludes fb-state.json.
  2. Run a task:
         python browser_worker.py --task example-task.json --out result.json
  3. Check the format without Facebook:
         python browser_worker.py --task example-task.json --out result.json --demo

Setup:
    pip install -r requirements-browser.txt
    playwright install chromium

Honest limits:
- Facebook changes its page layout often, so the post selectors below
  are best-effort and may need updating. When a selector finds nothing,
  the worker returns status "error" instead of fake posts.
- Post dates on Facebook are often relative ("2h", "Yesterday"), so the
  date filter only applies when a real datetime can be read from the
  page. Posts without a readable date are kept and flagged by leaving
  created_at empty.
- Go slowly: fetch at most 20 posts per task, scroll a few times only,
  and only read groups you are allowed to view. Do not bulk-scrape.
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from worker import extract_fields, in_date_range, load_json, save_json  # noqa: E402


def _playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -r requirements-browser.txt "
            "then: playwright install chromium"
        ) from exc
    return sync_playwright


def login(state_path: Path) -> None:
    """Open a headed browser, let the user log in, then save the session."""
    with _playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")
        print("Log in to Facebook in the browser window, then press Enter here.")
        input()
        context.storage_state(path=str(state_path))
        browser.close()
    print(f"Saved login state to {state_path}. Keep this file private.")


def _post_datetime(article) -> str:
    """Best-effort: read a <time datetime=...> inside a post, else ''."""
    try:
        t = article.locator("time").first
        if t.count():
            return t.get_attribute("datetime") or ""
    except Exception:
        pass
    return ""


def fetch_group_posts(group_id: str, keyword: str, limit: int,
                      date_from, date_to, state_path: Path) -> list[dict]:
    """Open the group feed in a logged-in browser and extract posts."""
    if not state_path.exists():
        raise RuntimeError(
            f"Missing login state {state_path}. Run first: python browser_worker.py --login"
        )
    url = f"https://www.facebook.com/groups/{group_id}/"
    if keyword:
        # Facebook group search page; if it redirects, we fall back to the feed.
        url = f"https://www.facebook.com/groups/{group_id}/search/?q={keyword}"
    posts: list[dict] = []
    seen: set[str] = set()
    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        # Scroll a few times to load more posts. Slow on purpose.
        for _ in range(4):
            if len(posts) >= limit:
                break
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(2500)
            articles = page.locator("div[role='article']")
            count = min(articles.count(), 40)
            for i in range(count):
                if len(posts) >= limit:
                    break
                article = articles.nth(i)
                try:
                    text = article.inner_text(timeout=2000)
                except Exception:
                    continue
                if not text or len(text) < 10:
                    continue
                link = ""
                try:
                    a = article.locator("a[href*='/permalink/']").first
                    if a.count():
                        link = a.get_attribute("href") or ""
                        if link.startswith("/"):
                            link = "https://www.facebook.com" + link
                except Exception:
                    pass
                key = link or text[:120]
                if key in seen:
                    continue
                seen.add(key)
                created = _post_datetime(article)
                if not in_date_range(created, date_from, date_to):
                    continue
                if keyword and keyword.lower() not in text.lower():
                    continue
                posts.append({
                    "post_id": link.rsplit("/", 2)[-2] if "/permalink/" in link else "",
                    "post_url": link,
                    "created_at": created,
                    "content": text[:4000],
                    "react_count": 0,
                    "comment_count": 0,
                    "extracted": {},
                })
        browser.close()
    if not posts:
        raise RuntimeError(
            "No posts could be read from the group page. Facebook may have "
            "changed its layout, asked for a login check, or the group is "
            "not viewable with this account. Try --login again."
        )
    return posts


def run_task(task: dict, state_path: Path, demo: bool) -> dict:
    task_id = task.get("task_id", "")
    action = task.get("action", "")
    if demo:
        sample = load_json(Path(__file__).parent / "example-result.json")
        sample["task_id"] = task_id or sample.get("task_id", "TASK-DEMO")
        sample["status"] = "success"
        sample["summary"] = "Demo mode: replayed bundled example result, no browser was opened."
        return sample
    if action != "search_group_posts":
        return {"task_id": task_id, "status": "error",
                "errors": ["Browser worker v1 only supports action search_group_posts."],
                "posts": [], "groups": [],
                "summary": "Task rejected: browser worker supports search_group_posts only."}
    group_id = task.get("group_id") or task.get("group_name", "")
    if not group_id:
        return {"task_id": task_id, "status": "error",
                "errors": ["Missing required field: group_id or group_name."],
                "posts": [], "groups": [],
                "summary": "Task rejected: missing group_id or group_name."}
    limit = max(1, min(int(task.get("limit", 10) or 10), 20))
    wanted = task.get("fields_to_extract", []) or []
    keyword = (task.get("post_keyword") or "").strip()
    try:
        raw = fetch_group_posts(group_id, keyword, limit,
                                task.get("date_from"), task.get("date_to"), state_path)
        for post in raw:
            post["extracted"] = extract_fields(post.get("content", ""), wanted)
        return {"task_id": task_id, "status": "success", "errors": [],
                "group": {"id": str(group_id), "name": "",
                          "url": f"https://www.facebook.com/groups/{group_id}/"},
                "posts": raw, "groups": [],
                "summary": f"Found {len(raw)} posts in group {group_id} (browser)."}
    except Exception as exc:  # noqa: BLE001 - worker must return JSON, not crash
        return {"task_id": task_id, "status": "error", "errors": [str(exc)],
                "posts": [], "groups": [], "summary": f"Task failed: {exc}"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AGENT-V1 browser worker (experimental)")
    parser.add_argument("--task", default="example-task.json")
    parser.add_argument("--out", default="result.json")
    parser.add_argument("--login", action="store_true", help="Log in once and save fb-state.json")
    parser.add_argument("--demo", action="store_true", help="Replay bundled example, no browser")
    args = parser.parse_args(argv)
    root = Path(__file__).parent
    state_path = root / "fb-state.json"
    if args.login:
        try:
            login(state_path)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    task_path = Path(args.task)
    if not task_path.exists():
        print(f"Task file not found: {task_path}", file=sys.stderr)
        return 2
    result = run_task(load_json(task_path), state_path, demo=args.demo)
    save_json(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
