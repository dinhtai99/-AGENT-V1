# AGENT-V1 — Standalone version (runs without Muse)

This is the independent version of the AGENT-V1 worker. It runs on your
own Windows machine with Python. No Muse account is needed to run it.

> **Important (2026-11-09): Meta removed the Facebook Groups API on
> 22 April 2024 (Graph API v19.0 changelog: the Groups API and the
> `publish_to_groups` / `groups_access_member_info` permissions were
> removed from every API version). There is no token-based worker any
> more. The browser worker (`browser_worker.py`) is the only
> standalone path. Use it.

## What it does

Same 5-step workflow as the Muse worker, but with plain Python:

1. Read one task JSON (`--task`).
2. Open the Facebook group feed in a logged-in browser.
3. Read posts from that feed (latest first), filter by keyword and by
   date locally. Date filter uses `date_from` / `date_to` in the task
   (YYYY-MM-DD, inclusive). Posts without a readable date are kept.
4. Extract the fields the task asked for (phone, price, location, time,
   level) with regex. Missing fields become `null`, never guessed.
5. Write one result JSON (`--out`) in the `task-schema.json` format.

## Setup (Windows)

1. Install Python 3.10 or newer from python.org, tick "Add Python to PATH".
2. Open PowerShell in this folder and install the browser worker:
   ```
   pip install -r requirements-browser.txt
   playwright install chromium
   ```
3. Log in once so the browser worker can reuse your Facebook session:
   ```
   python browser_worker.py --login
   ```
   A browser window opens: log in to Facebook by hand, then press Enter
   in the terminal. The session is saved to `fb-state.json`. Never share
   or commit that file. `.gitignore` already excludes it.

## Run (browser worker — primary path)

```
python browser_worker.py --task task-test-browser-tuyen.json --out result.json
python browser_worker.py --task example-task.json --out result.json
```

Check the format without Facebook first:
```
python browser_worker.py --task example-task.json --out result.json --demo
```
Demo replays the bundled example result and opens no browser.

## Browser version (primary path, experimental)

The browser worker reads Facebook through a real logged-in browser, so
it needs no API token. It only supports `search_group_posts` for now
(finding groups by keyword is not supported yet), goes slowly on
purpose (a few scrolls, at most 20 posts), and its post selectors are
best-effort: Facebook changes its layout often, so if it finds nothing
it returns an error instead of fake posts. Post dates on Facebook are
often relative, so the date filter only applies when a real datetime
can be read from the page.

Real risks of browser automation, stated plainly:
- Facebook may change its page layout and break the post selectors.
- Facebook may ask for a login check (2FA / checkpoint) at any time;
  if so, log in again with `--login`.
- Automating a logged-in account carries account risk. Keep each task
  small, run it only when you need results, and do not bulk-scrape.

## Extra filters (optional task fields)

Besides keyword and date, a task can filter further. All are optional;
a post that lacks the info a filter needs is dropped, never guessed.

- `levels`: e.g. `["TB+", "TBK"]` — only posts with one of these levels.
- `min_price` / `max_price`: VND numbers, e.g. `40000` / `100000` —
  only posts with a price in that range ("65k" = 65000, "2 triệu" = 2000000).
- `location_keyword`: e.g. `"Trịnh Văn Bô"` — must appear in the
  content or the extracted location.
- `exclude_keywords`: e.g. `["bán cầu"]` — posts containing any of
  these words are dropped.
- `only_new`: `true` — only return posts never seen before. Seen post
  ids are remembered in `seen.json` (gitignored, stays on your machine).
  First run with `only_new` returns everything matching, later runs
  only the new ones.

## Customer profile — the target customer comes from the orchestrator

The worker does not decide who the customer is. The manager agent sends
one free-text description in the task as `customer_profile`, e.g.:

```
"Nữ chơi cầu lông trình TB+, ở Hà Nội, cần sân buổi tối, giá dưới 100k"
```

The worker pulls the criteria out of that text and keeps a post when it
**contradicts none of them and matches at least one**:

- level: `TB+` in the text -> post stating TBK contradicts it; a post
  stating no level is unknown, not a fail.
- gender: `nữ` / `nam` in the text -> post stating the other gender
  contradicts it.
- location: words after `ở` / `tại` / `khu vực` -> post mentioning it
  matches; not mentioning it is unknown.
- price: `dưới 100k` -> max price 100.000đ; a stated price above it
  contradicts it; no stated price is unknown.
- time of day: `buổi tối` -> 17h-23h; `buổi sáng` -> 6h-11h; `buổi chiều` -> 12h-17h.

Missing info is never guessed and never a reason to drop a post on its
own — real posts rarely state every detail. Each kept post carries
`profile_match` with `matched`, `contradicted` and `unknown` lists, so
the manager agent can see why it was kept and rank candidates. No extra
AI or API key is needed for this: it reuses the same extractors as the
other filters. `example-task-customer.json` is a ready example.

Keyword matching is accent-insensitive everywhere: `post_keyword`
"tuyển" also matches posts written "tuyen" without accents, and the
same applies to `location_keyword` / `exclude_keywords`.

## Watch mode — auto-scan for new posts

### Scanning several groups in one run

One task can list several groups in `group_ids`. The worker scans them
one after another (never in parallel), pausing `group_pause_seconds`
(default 20) between groups. If one group fails (login check, layout),
it is skipped and the run continues with the next group; the failure is
reported in `errors` and in that group's entry in `groups`, without
failing the whole run. Each group has its own seen-post memory in
`seen.json`, so a post is only reported once per group. A post shared
into two groups is two different posts and is reported twice.

Keep the total load modest: 5 groups every 30 minutes means 10 group
page loads per hour. Start with 2–3 groups, watch for login checks for
a day, then add more. `example-task-multi.json` scans 5 Hanoi badminton
groups with the customer profile, every 30 minutes.

Run the same task again and again, hands-free:

```
python browser_worker.py --task example-task-watch.json --out result.json --watch
```

It scans, waits `watch_interval_minutes` from the task file (default
30, minimum 5), then scans again, until you press Ctrl+C. Each scan
writes the latest result to `--out`, and every newly found post is
appended to `watch-new.jsonl` (one JSON object per line, with the scan
time). Combine with `"only_new": true` so each scan only reports posts
you have not seen yet.

Watch honestly: scanning too often raises the account risk above, and
the browser worker is slow by design. 20–60 minutes between scans is
the sensible range. Do not run watch mode on many groups at once, and
stop it when you have what you need. `example-task-watch.json` is a
ready watch task: keyword "tuyển", levels TB+/TBK, price 40.000–
100.000đ, only new posts, every 20 minutes.

## Running 24/24

Watch mode runs for as long as its PowerShell window stays open. For
true 24/24 running, the machine must stay on (disable sleep) with that
window open. If the machine restarts, start watch again with the same
command — `seen.json` remembers the posts already seen, so it will not
report the old ones again. A small always-on server (VPS) is the
sturdier option for real 24/24 operation; the worker needs nothing
else installed beyond Python, Playwright and Chromium.

### Watchdog: restart the worker when it dies

`watchdog.py` watches the worker and restarts it only when it died
unexpectedly (crash, reboot). It never restarts a worker that stopped
on purpose — check `watch-status.json` first:

- `status: "stopped"`, `stop_reason: "session_expired"` — the Facebook
  session is dead. Restarting would fail identically, so run
  `python browser_worker.py --login` first, then start the watch again.
- `status: "stopped"`, `stop_reason: "repeated_errors"` — 3 scans
  failed in a row (layout change, network). Fix the cause first.
- `status: "running"` / `"error"` with a fresh `updated_at` — the
  worker is alive; leave it alone.

Start everything with one double-click:

```
start-watch.bat
```

That starts the watchdog, which starts the worker
(`example-task-multi.json`). To survive reboots, add the watchdog to
Windows Task Scheduler: trigger "At log on", action
`python watchdog.py --task example-task-multi.json`, start in the
AGENT-V1 folder. The watchdog also restarts the worker after a
reboot, because `seen.json` still remembers the old posts.

Stability details built into the watch loop:

- **Jitter:** each interval is randomized by
  `watch_interval_jitter_percent` (default 20, so a 30-minute interval
  sleeps 24–36 minutes), the pause between groups by ±50%, and scroll
  waits by a few hundred ms — scans don't run on a mechanical clock.
- **One bad scan never kills the loop:** exceptions are caught and
  counted; the loop waits out the interval and tries again.
- **Heartbeat:** every scan writes `watch-status.json` (last scan
  time, status, consecutive errors, totals) — that is what the
  watchdog reads.
- **No auto re-login:** the worker cannot log in by itself without a
  stored password, and passwords are never stored. When the session
  expires the loop stops cleanly and says so in `watch-status.json`.

## Limits (same as the Muse worker)

- Only groups the logged-in account can view: public groups, or groups
  you joined.
- Hidden private groups you are not a member of are not accessible.
- At most 20 posts per `search_group_posts` task. No bulk scraping.
- Keyword filtering happens locally after fetching the latest posts,
  because the browser worker reads the group feed, not a search API.

## Files

- `browser_worker.py` — browser worker (the standalone worker).
- `watchdog.py` — restarts the watch worker if it dies (never a worker
  that stopped on purpose). `start-watch.bat` starts the watchdog.
- `filters.py` — post extraction and filtering (no Facebook calls).
- `requirements-browser.txt` — only `playwright`.
- `task-test-browser-tuyen.json` — ready test: keyword "tuyển" in the
  badminton group, 2026-11-09, max 5 posts.
- `example-task-watch.json` — ready watch task: only new "tuyển" posts,
  levels TB+/TBK, price 40.000–100.000đ, every 20 minutes.
- `example-task-watch-loose.json` — same watch task with looser filters:
  only new "tuyển" posts, no level or price filter, every 20 minutes.
- `example-task-customer.json` — watch task with `customer_profile`:
  nữ, trình TB+, ở Hà Nội, sân buổi tối, giá dưới 100k.
- `example-task-multi.json` — watch task scanning 5 Hanoi badminton
  groups in turn (`group_ids`), same customer profile, every
  30 minutes, 20 seconds pause between groups.
- `example-task.json` — sample task.
- `task-schema.json` — input/output contract.
- `AGENT.md` / `WORKFLOW.md` — the worker rules and process.
