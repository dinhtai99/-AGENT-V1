#!/usr/bin/env python3
"""
AGENT-V1 — Browser worker (the standalone worker).

Reads Facebook through a real logged-in browser. Meta removed the
Groups API on 22 April 2024, so this browser path is the only
standalone way to read group posts — there is no token-based worker.

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
import os
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from filters import (contains_vn, extract_fields, in_date_range, load_json,
                     post_matches_filters, save_json)  # noqa: E402

# How many failed scans in a row before the watch loop stops on purpose
# (something is structurally wrong — layout change, network down — and
# hammering Facebook won't fix it). The watchdog restarts a *crashed*
# worker, never one that stopped on purpose; see watchdog.py.
MAX_CONSECUTIVE_ERRORS = 3

# Strings that mean the saved Facebook session is dead. A scan that hits
# one of these will fail identically every time, so the loop stops at
# once instead of retrying until the next scan.
_SESSION_MARKERS = ("login/checkpoint", "Missing login state")


def _is_session_error(msg: str) -> bool:
    """True when a scan error means the login session expired."""
    return any(m in (msg or "") for m in _SESSION_MARKERS)


def _jittered(base: float, pct: float) -> float:
    """Randomize a wait by +/-pct% so scans don't run on a mechanical clock.

    Facebook notices perfectly regular page loads. A 30-minute interval
    with 20% jitter sleeps 24-36 minutes; a 20-second group pause with
    50% jitter sleeps 10-30 seconds. pct is clamped to 0-50.
    """
    if pct <= 0 or base <= 0:
        return float(base)
    span = base * min(pct, 50) / 100.0
    return base + random.uniform(-span, span)


def _write_status(root: Path, data: dict) -> None:
    """Best-effort: write watch-status.json (heartbeat + last scan state).

    The watchdog reads this file to decide whether the worker is alive.
    A failed write must never kill a scan.
    """
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
    # locally below, the same way filters.py does it.
    url = f"https://www.facebook.com/groups/{group_id}/"
    posts: list[dict] = []
    seen: set[str] = set()
    # Did we see any article with a real post link at all? This
    # distinguishes a real page problem (no post links anywhere) from
    # "the visible posts just don't contain the keyword right now" —
    # the second is a successful scan with 0 matches, not a failure.
    saw_post_link = False
    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(random.randint(4000, 6000))
        if "login" in page.url or "checkpoint" in page.url:
            browser.close()
            raise RuntimeError(
                "Facebook redirected to a login/checkpoint page. "
                "The saved session may have expired. Run again: "
                "python browser_worker.py --login"
            )
        # Scroll a few times to load more posts. Slow on purpose, and
        # slightly irregular on purpose: fixed waits look mechanical.
        for _ in range(4):
            if len(posts) >= limit:
                break
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(random.randint(2000, 3500))
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
                saw_post_link = True
                key = link
                if key in seen:
                    continue
                seen.add(key)
                created = _post_datetime(article)
                if not in_date_range(created, date_from, date_to):
                    continue
                if keyword and not contains_vn(text, keyword):
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
    if not posts and not saw_post_link:
        raise RuntimeError(
            "No posts could be read from the group page. Facebook may have "
            "changed its layout, asked for a login check, or the group is "
            "not viewable with this account. Try --login again, or run with "
            "--debug and send debug.json to check what the page actually shows."
        )
    # No post link at all -> page problem (above). Post links seen but
    # none matched the keyword/date -> successful scan, 0 matching posts.
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


def _scan_one_group(task: dict, group_id: str, state_path: Path,
                    seen: dict, debug: bool = False) -> tuple[list[dict], int, str | None]:
    """Scan one group: fetch, extract, filter, drop already-seen posts.

    Returns (kept_posts, skipped_seen_count, error_or_None). A failure
    here must not stop the other groups in a multi-group task.
    """
    limit = max(1, min(int(task.get("limit", 10) or 10), 20))
    wanted = task.get("fields_to_extract", []) or []
    keyword = (task.get("post_keyword") or "").strip()
    try:
        raw = fetch_group_posts(group_id, keyword, limit,
                                task.get("date_from"), task.get("date_to"), state_path,
                                debug=debug)
    except Exception as exc:  # noqa: BLE001 - one bad group must not kill the run
        return [], 0, str(exc)
    for post in raw:
        post["group_id"] = str(group_id)
        post["extracted"] = extract_fields(post.get("content", ""), wanted)
    kept = [post for post in raw if post_matches_filters(post, task)]
    # only_new: keep only posts never seen before for this group.
    # Seen ids live in seen.json next to this file (gitignored).
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
    return kept, skipped_seen, None


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
    group_ids = task.get("group_ids") or []
    if isinstance(group_ids, str):
        group_ids = [group_ids]
    group_ids = [str(g).strip() for g in group_ids if str(g).strip()]
    if not group_ids:
        single = task.get("group_id") or task.get("group_name", "")
        if single:
            group_ids = [str(single).strip()]
    if not group_ids:
        return {"task_id": task_id, "status": "error",
                "errors": ["Missing required field: group_id or group_ids."],
                "posts": [], "groups": [],
                "summary": "Task rejected: missing group_id or group_ids."}
    # Pause between groups so the run looks like browsing one group at a
    # time, not hammering many pages at once. Default 20 seconds; 0 means
    # no pause (explicit None check: "or 20" would swallow a 0).
    raw_pause = task.get("group_pause_seconds", 20)
    pause = 20 if raw_pause is None else max(0, min(int(raw_pause), 300))
    seen: dict = {}
    if task.get("only_new"):
        seen = load_seen(Path(__file__).parent / "seen.json")
    posts: list[dict] = []
    groups_out: list[dict] = []
    errors: list[str] = []
    skipped_total = 0
    for i, group_id in enumerate(group_ids):
        if i > 0 and pause:
            # Jitter the pause +/-50% (20s -> 10-30s) so group visits
            # don't land on a perfectly regular rhythm.
            time.sleep(_jittered(pause, 50))
        kept, skipped, error = _scan_one_group(task, group_id, state_path, seen, debug=debug)
        skipped_total += skipped
        posts.extend(kept)
        groups_out.append({"id": str(group_id),
                           "url": f"https://www.facebook.com/groups/{group_id}/",
                           "found": len(kept),
                           "error": error or ""})
        if error:
            errors.append(f"Group {group_id}: {error}")
    if task.get("only_new"):
        save_seen(Path(__file__).parent / "seen.json", seen)
    ok_count = sum(1 for g in groups_out if not g["error"])
    new_note = f" ({skipped_total} already seen skipped)" if skipped_total else ""
    if ok_count == 0:
        return {"task_id": task_id, "status": "error", "errors": errors,
                "group": {"id": str(group_ids[0]), "name": "",
                          "url": f"https://www.facebook.com/groups/{group_ids[0]}/"},
                "posts": [], "groups": groups_out,
                "summary": f"Task failed in all {len(group_ids)} group(s): {errors[0] if errors else 'unknown error'}"}
    return {"task_id": task_id, "status": "success", "errors": errors,
            "group": {"id": str(group_ids[0]), "name": "",
                      "url": f"https://www.facebook.com/groups/{group_ids[0]}/"},
            "posts": posts, "groups": groups_out,
            "summary": f"Found {len(posts)} posts in {ok_count}/{len(group_ids)} group(s) (browser){new_note}."}


def watch_loop(task: dict, state_path: Path, out_path: Path, root: Path,
               demo: bool = False, debug: bool = False,
               sleep_fn=time.sleep, run_fn=None, max_cycles=None) -> int:
    """Repeat the task until Ctrl+C or a stop condition (see below).

    Stability rules for 24/24 running:
    - One bad scan never kills the loop: exceptions are caught, counted,
      and the loop waits out the normal interval before trying again.
    - A dead login session stops the loop at once (every later scan
      would fail identically). watch-status.json says "stopped" with
      stop_reason "session_expired" — run --login, then start again.
    - 3 failed scans in a row also stop the loop (stop_reason
      "repeated_errors"): something structural is wrong.
    - Every scan writes watch-status.json (heartbeat): last scan time,
      status, consecutive errors, totals. watchdog.py reads it.
    - The interval is jittered +/-watch_interval_jitter_percent
      (default 20) so scans don't run on a mechanical clock.
    Returns 0 on a clean stop, 1 when stopped by a stop condition.
    sleep_fn / run_fn / max_cycles exist so tests can drive the loop
    without a browser or real waiting.
    """
    if run_fn is None:
        run_fn = run_task
    interval = max(5, int(task.get("watch_interval_minutes", 30) or 30))
    raw_jitter = task.get("watch_interval_jitter_percent", 20)
    jitter_pct = 20 if raw_jitter is None else max(0, min(float(raw_jitter), 50))
    print(f"Watching every ~{interval} minute(s) (+/-{jitter_pct:.0f}%). Press Ctrl+C to stop.")
    print("Tip: set \"only_new\": true in the task so each scan only reports new posts.")
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
                result = {"task_id": task.get("task_id", ""), "status": "error",
                          "errors": [f"Scan crashed: {exc}"], "posts": [],
                          "summary": f"Scan crashed: {exc}"}
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
                            f.write(json.dumps({"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                "task_id": result.get("task_id", ""),
                                                **post}, ensure_ascii=False) + "\n")
                    print(f"  -> {len(new_posts)} new post(s) appended to watch-new.jsonl")
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
            _write_status(root, {
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "task_id": task.get("task_id", ""),
                "status": "stopped" if stop_reason else status,
                "stop_reason": stop_reason,
                "last_summary": last_summary,
                "consecutive_errors": consecutive_errors,
                "scans_total": scans_total,
                "found_total": found_total,
                "next_scan_in_seconds": round(wait_seconds) if not stop_reason else None,
            })
            if stop_reason:
                if stop_reason == "session_expired":
                    print("STOP: Facebook session expired (login/checkpoint redirect).")
                    print("Run again: python browser_worker.py --login  (then restart the watch)")
                else:
                    print(f"STOP: {consecutive_errors} failed scans in a row. "
                          "Check the errors above / debug.json, fix the cause, then restart.")
                return 1
            sleep_fn(wait_seconds)
    except KeyboardInterrupt:
        stop_reason = "user_stopped"
        print("\nWatch stopped.")
    finally:
        if stop_reason:
            _write_status(root, {
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "task_id": task.get("task_id", ""),
                "status": "stopped",
                "stop_reason": stop_reason,
                "last_summary": last_summary,
                "consecutive_errors": consecutive_errors,
                "scans_total": scans_total,
                "found_total": found_total,
                "next_scan_in_seconds": None,
            })
        try:
            pid_path.unlink(missing_ok=True)
        except Exception:
            pass
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AGENT-V1 browser worker (experimental)")
    parser.add_argument("--task", default="example-task.json")
    parser.add_argument("--out", default="result.json")
    parser.add_argument("--login", action="store_true", help="Log in once and save fb-state.json")
    parser.add_argument("--demo", action="store_true", help="Replay bundled example, no browser")
    parser.add_argument("--debug", action="store_true", help="Write debug.json with what the page actually shows")
    parser.add_argument("--watch", action="store_true",
                        help="Repeat the task every watch_interval_minutes (from the task file, default 30, min 5) until Ctrl+C")
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
    task = load_json(task_path)
    if not args.watch:
        result = run_task(task, state_path, demo=args.demo, debug=args.debug)
        save_json(Path(args.out), result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    # Watch mode: scan again and again, only new posts are reported.
    # Combine with "only_new": true in the task so repeats are skipped.
    # Stability rules (jitter, heartbeat, stop conditions) live in
    # watch_loop(); watchdog.py restarts the worker if it crashes.
    return watch_loop(task, state_path, Path(args.out), root,
                      demo=args.demo, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
