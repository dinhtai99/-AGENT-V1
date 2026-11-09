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
- It reads the group FEED only (never the search page, which mixes in
  people/pages suggestions) and only accepts items with a real post
  permalink, so suggestions and ads are skipped.
- Post dates on Facebook are often relative ("2h", "Yesterday"), so the
  date filter only applies when a real datetime can be read from the
  page. Posts without a readable date are kept and flagged by leaving
  created_at empty.
- Go slowly: fetch at most 20 posts per task, scroll a few times only,
  and only read groups you are allowed to view. Do not bulk-scrape.
"""

import argparse
import json
import re
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
    return sync_playwright()


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


def _post_link(article) -> str:
    """Best-effort: find a real post link inside an article.

    Facebook uses more than one format: /permalink/<id>/ and
    /posts/<id>/ are both real post links. Anything else is not a post.
    """
    for pattern in ("/permalink/", "/posts/"):
        try:
            a = article.locator(f"a[href*='{pattern}']").first
            if a.count():
                href = a.get_attribute("href") or ""
                if href.startswith("/"):
                    href = "https://www.facebook.com" + href
                # Drop tracking query params (?__cft__, ?__tn__, ...) so
                # the saved link is a clean, shareable post URL.
                href = href.split("?")[0]
                return href
        except Exception:
            continue
    return ""


def _dump_debug(page, articles, count: int) -> None:
    """Save what the page actually contains, so a failed run can be diagnosed.

    Writes debug.json next to this file: final URL, how many articles
    were found, the first links inside them, and short text samples.
    No session cookies or tokens are written.
    """
    sample_links: list[str] = []
    sample_texts: list[str] = []
    try:
        for i in range(min(count, 10)):
            try:
                hrefs = articles.nth(i).locator("a[href]").evaluate_all(
                    "els => els.slice(0, 5).map(e => e.getAttribute('href'))")
                sample_links.extend(h for h in hrefs if h)
            except Exception:
                pass
            try:
                sample_texts.append(articles.nth(i).inner_text(timeout=1000)[:200])
            except Exception:
                pass
    except Exception:
        pass
    data = {"url": page.url, "article_count": count,
            "sample_links": sample_links[:30], "sample_texts": sample_texts[:5]}
    Path(__file__).parent.joinpath("debug.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[debug] wrote debug.json: {count} articles at {page.url}")


def fetch_group_posts(group_id: str, keyword: str, limit: int,
                      date_from, date_to, state_path: Path,
                      debug: bool = False) -> list[dict]:
    """Open the group feed in a logged-in browser and extract posts."""
    if not state_path.exists():
        raise RuntimeError(
            f"Missing login state {state_path}. Run first: python browser_worker.py --login"
        )
    # Always read the group FEED, never the search page: the search page
    # mixes in people/pages suggestions (e.g. profiles named like the
    # keyword), which are not group posts. Keyword filtering happens
    # locally below, the same way worker.py does it.
    url = f"https://www.facebook.com/groups/{group_id}/"
    posts: list[dict] = []
    seen: set[str] = set()
    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        if "login" in page.url or "checkpoint" in page.url:
            browser.close()
            raise RuntimeError(
                "Facebook redirected to a login/checkpoint page. "
                "The saved session may have expired. Run again: "
                "python browser_worker.py --login"
            )
        # Scroll a few times to load more posts. Slow on purpose.
        for _ in range(4):
            if len(posts) >= limit:
                break
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(2500)
            articles = page.locator("div[role='article']")
            count = min(articles.count(), 40)
            if debug and not posts:
                _dump_debug(page, articles, count)
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
                link = _post_link(article)
                # A real group post always has a post link. Anything
                # without one (people suggestions, ads, UI cards) is
                # not a post — skip it instead of returning it as one.
                if "/permalink/" not in link and "/posts/" not in link:
                    continue
                key = link
                if key in seen:
                    continue
                seen.add(key)
                created = _post_datetime(article)
                if not in_date_range(created, date_from, date_to):
                    continue
                if keyword and keyword.lower() not in text.lower():
                    continue
                post_id = ""
                m = re.search(r"/(?:permalink|posts)/(\d+)", link)
                if m:
                    post_id = m.group(1)
                posts.append({
                    "post_id": post_id,
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
            "not viewable with this account. Try --login again, or run with "
            "--debug and send debug.json to check what the page actually shows."
        )
    return posts


def run_task(task: dict, state_path: Path, demo: bool, debug: bool = False) -> dict:
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
                                task.get("date_from"), task.get("date_to"), state_path,
                                debug=debug)
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
    parser.add_argument("--debug", action="store_true", help="Write debug.json with what the page actually shows")
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
    result = run_task(load_json(task_path), state_path, demo=args.demo, debug=args.debug)
    save_json(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
