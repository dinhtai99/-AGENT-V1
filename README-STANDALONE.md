# AGENT-V1 — Standalone version (runs without Muse)

This is the independent version of the AGENT-V1 worker. It runs on your
own Windows machine with Python. No Muse account is needed to run it.

> **Important (2026-11-09): Meta removed the Facebook Groups API on
> 22 April 2024 (Graph API v19.0 changelog: the Groups API and the
> `publish_to_groups` / `groups_access_member_info` permissions were
> removed from every API version). The token-based worker (`worker.py`)
> calls exactly those removed endpoints, so it is expected to fail with
> a Meta error. The browser worker (`browser_worker.py`) is the primary
> standalone path. Use it first.

## What it does

Same 5-step workflow as the Muse worker, but with plain Python:

1. Read one task JSON (`--task`).
2. Resolve the Facebook group, or search groups by keyword.
3. Fetch posts from that group (latest, then filter by keyword and by
   date locally). Date filter uses `date_from` / `date_to` in the task
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

## Token worker (worker.py) — expected to fail, kept for reference

`worker.py` reads groups through the Graph API with a token in `.env`
(`FB_ACCESS_TOKEN`). Because Meta removed the Groups API on 22 April
2024, its calls (`/search?type=group`, `/{group_id}`, `/{group_id}/feed`)
are expected to return a Meta error. The worker handles that honestly:
it returns `status: "error"` with Meta's message instead of fake data.
Do not spend time creating a token for group reading — it will not
restore access.

In the Muse version, Facebook access comes from the linked Facebook
account instead of a token, which is why the Muse worker still works.

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

## Limits (same as the Muse worker)

- Only groups the logged-in account can view: public groups, or groups
  you joined.
- Hidden private groups you are not a member of are not accessible.
- At most 20 posts per `search_group_posts` task. No bulk scraping.
- Keyword filtering happens locally after fetching the latest posts,
  because the browser worker reads the group feed, not a search API.

## Files

- `worker.py` — the token worker. Kept for reference; its group calls
  are expected to fail since the Groups API was removed (see above).
- `browser_worker.py` — browser worker (primary standalone path).
- `requirements.txt` — only `requests` (token worker).
- `requirements-browser.txt` — only `playwright`.
- `.env.example` — copy to `.env`, fill in your token (token worker only).
- `task-test-browser-tuyen.json` — ready test: keyword "tuyển" in the
  badminton group, 2026-11-09, max 5 posts.
- `example-task.json` / `example-task-find-groups.json` — sample tasks.
- `task-schema.json` — input/output contract.
- `AGENT.md` / `WORKFLOW.md` — the worker rules and process.
