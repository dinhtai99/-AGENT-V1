# AGENT-V1 — Standalone version (runs without Muse)

This is the independent version of the AGENT-V1 worker. It runs on your
own Windows machine with Python. No Muse account is needed to run it.

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
2. Open PowerShell in this folder:
   ```
   pip install -r requirements.txt
   copy .env.example .env
   ```
3. Open `.env` in Notepad / VS Code and paste your Facebook token:
   ```
   FB_ACCESS_TOKEN=paste-your-token-here
   ```
   Never share this file or commit it. `.gitignore` already excludes it.

## Run

```
python worker.py --task example-task.json --out result.json
python worker.py --task example-task-find-groups.json --out result.json
```

Check the format without Facebook first:
```
python worker.py --task example-task.json --out result.json --demo
```
Demo replays the bundled example result and makes no Facebook call.

## Getting a Facebook token

Use the Graph API Explorer or your own Meta app to create a User token
with access to the groups you manage or belong to. Group access on the
Graph API is restricted and may need app review. If Meta refuses a call,
the worker returns `status: "error"` with Meta's message instead of
fake data. That is intentional.

In the Muse version, Facebook access comes from the linked Facebook
account instead of a token. The standalone version needs a token
because it runs outside Muse.

## Browser version (fallback, experimental)

When the Graph API refuses group access, use the browser worker instead.
It reads Facebook through a real logged-in browser, so it needs no API
token.

```
pip install -r requirements-browser.txt
playwright install chromium
python browser_worker.py --login
python browser_worker.py --task example-task.json --out result.json
```

`--login` opens a browser window: log in to Facebook by hand, then press
Enter here to save the session to `fb-state.json`. Never share or commit
that file. The browser worker only supports `search_group_posts` for now,
goes slowly on purpose (a few scrolls, at most 20 posts), and its post
selectors are best-effort: Facebook changes its layout often, so if it
finds nothing it returns an error instead of fake posts. Post dates on
Facebook are often relative, so the date filter only applies when a real
datetime can be read from the page.

## Limits (same as the Muse worker)

- Only groups the token can view: public groups, or groups you joined.
- Hidden private groups you are not a member of are not accessible.
- At most 20 posts per `search_group_posts` task, 25 groups per
  `find_groups` task. No bulk scraping.
- Keyword filtering happens locally after fetching the latest posts,
  because the Graph API does not reliably search group post text.

## Files

- `worker.py` — the worker. Run it, don't edit it unless you need to.
- `browser_worker.py` — browser fallback (experimental, Playwright).
- `requirements.txt` — only `requests`.
- `requirements-browser.txt` — only `playwright`.
- `.env.example` — copy to `.env`, fill in your token.
- `example-task.json` / `example-task-find-groups.json` — sample tasks.
- `task-schema.json` — input/output contract.
- `AGENT.md` / `WORKFLOW.md` — the worker rules and process.
