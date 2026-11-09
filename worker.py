#!/usr/bin/env python3
"""
AGENT-V1 — Standalone worker (Python, no Muse needed).

Reads one task JSON, runs the 5-step workflow, writes one result JSON.
See task-schema.json for the input/output contract.
See README-STANDALONE.md for setup and Facebook limits.

Usage (Windows PowerShell / cmd):
    python worker.py --task example-task.json --out result.json
    python worker.py --task example-task-find-groups.json --out result.json
    python worker.py --task example-task.json --out result.json --demo

The worker never invents posts. Without a Facebook token it returns
status "error" and explains what is missing, except in --demo mode,
which replays the bundled example result so you can check the format.

NOTE (2026-11-09): Meta removed the Facebook Groups API on 22 April
2024 (Graph API v19.0 changelog). This worker's group calls
(/search?type=group, /{group_id}, /{group_id}/feed) are therefore
expected to fail with a Meta error. The browser worker
(browser_worker.py) is the primary standalone path; this file is kept
for reference and for its demo mode.
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

VERSION = "v19.0"


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ (no overwrite)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_date(value: str | None) -> date | None:
    """Parse YYYY-MM-DD (or an ISO datetime) to a date, else None."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def in_date_range(created_at: str, date_from: str | None, date_to: str | None) -> bool:
    """True when a post's date is inside [date_from, date_to]. Missing dates pass."""
    post_date = parse_date(created_at)
    if post_date is None:
        return True
    start = parse_date(date_from)
    end = parse_date(date_to)
    if start and post_date < start:
        return False
    if end and post_date > end:
        return False
    return True


# ---------------------------------------------------------------- extraction

PHONE_RE = re.compile(r"(?:\+84|0)\d{8,9}\b")
PRICE_RE = re.compile(r"(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ))", re.IGNORECASE)
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])h[0-5]\d?\b|\b([01]?\d|2[0-3])h\b|\b([01]?\d|2[0-3]):[0-5]\d\b")
LEVEL_RE = re.compile(r"\b(TBY|TBK|TB\+\+|TB\+|TB-|TB)(?![A-Za-z0-9])")


def norm_vn(text: str) -> str:
    """Lowercase Vietnamese text with diacritics stripped.

    "Tuyển" -> "tuyen", "Hà Nội" -> "ha noi", "đ" -> "d". Lets keyword
    matching catch posts written without accents, which is common.
    """
    text = unicodedata.normalize("NFD", str(text or "").lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.replace("đ", "d")


def contains_vn(haystack: str, needle: str) -> bool:
    """Accent-insensitive substring check."""
    return norm_vn(needle) in norm_vn(haystack)


def extract_fields(content: str, wanted: list[str]) -> dict:
    """Extract only the fields the task asked for. Missing -> None, never guessed."""
    text = content or ""
    out: dict = {}
    for field in wanted:
        if field == "phone":
            m = PHONE_RE.search(text)
            out["phone"] = m.group(0) if m else None
        elif field == "price":
            m = PRICE_RE.search(text)
            out["price"] = m.group(0).strip() if m else None
        elif field == "location":
            m = re.search(r"(?:Sân|Địa điểm|📌)\s*:?\s*([^\n,]{3,80})", text)
            out["location"] = m.group(1).strip() if m else None
        elif field == "time":
            m = TIME_RE.search(text)
            out["time"] = m.group(0) if m else None
        elif field == "level":
            out["level"] = sorted(set(LEVEL_RE.findall(text))) or None
        else:
            # Generic field: look for "field: value" in the text, else None.
            m = re.search(rf"{re.escape(field)}\s*:\s*([^\n]{{1,120}})", text, re.IGNORECASE)
            out[field] = m.group(1).strip() if m else None
    return out


def parse_price_vnd(text: str | None) -> int | None:
    """Convert an extracted price like '65k', '2 triệu', '100.000đ' to VND.

    Returns None when the text has no usable price. 'k'/'nghìn' = 1,000,
    'tr'/'triệu' = 1,000,000, 'đ'/'VND' = 1.
    """
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(triệu|nghìn|VND|tr|k|đ)?", str(text), re.IGNORECASE)
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        value = float(num)
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit in ("triệu", "tr"):
        value *= 1_000_000
    elif unit in ("nghìn", "k"):
        value *= 1_000
    return int(value)


def parse_customer_profile(profile: str) -> dict:
    """Pull matchable criteria out of a free-text customer description.

    The orchestrator sends one text, e.g.:
    "Nữ chơi cầu lông trình TB+, ở Hà Nội, cần sân buổi tối, giá dưới 100k"
    Only the criteria the text actually states are extracted:
    - levels: via LEVEL_RE, e.g. ["TB+"]
    - gender: "nữ" or "nam" (word match; both mentioned -> no criterion)
    - locations: words after ở / tại / khu vực / quận / sân, e.g. ["Hà Nội"]
    - price_min / price_max (VND): "từ X đến Y", "dưới X", "trên X"
    - time_of_day: "sáng" (6-11h), "chiều" (12-17h), "tối" (17-23h)
    Returns {} when the text yields nothing usable.
    """
    text = (profile or "").strip()
    if not text:
        return {}
    out: dict = {}
    levels = sorted(set(LEVEL_RE.findall(text)))
    if levels:
        out["levels"] = levels
    low = text.lower()
    has_nu = bool(re.search(r"\bnữ\b", low))
    has_nam = bool(re.search(r"\bnam\b", low))
    if has_nu and not has_nam:
        out["gender"] = "nữ"
    elif has_nam and not has_nu:
        out["gender"] = "nam"
    stop = {"cần", "tìm", "muốn", "và", "buổi", "giá", "chơi", "trình",
            "độ", "sân", "người", "giao", "lưu", "ca", "từ", "đến"}
    locations: list[str] = []
    for m in re.finditer(r"(?:ở|tại|khu vực|quận|huyện|phường|thành phố|sân)\s+", low):
        rest = low[m.end():]
        words = re.split(r"[\s,;]+", rest)
        picked: list[str] = []
        for word in words:
            word = word.strip(".,;:()")
            if not word or word in stop or len(picked) >= 3:
                break
            picked.append(word)
        if picked:
            locations.append(" ".join(picked))
    if locations:
        out["locations"] = locations
    m = re.search(r"từ\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ))\s*đến\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ))", low)
    if m:
        out["price_min"] = parse_price_vnd(m.group(1))
        out["price_max"] = parse_price_vnd(m.group(2))
    else:
        m = re.search(r"(?:dưới|không quá|tối đa)\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ))", low)
        if m:
            out["price_max"] = parse_price_vnd(m.group(1))
        m = re.search(r"(?:trên|từ)\s*(\d+(?:[.,]\d+)?\s*(?:triệu|nghìn|VND|tr|k|đ))", low)
        if m:
            out["price_min"] = parse_price_vnd(m.group(1))
    # "tối đa" / "tối thiểu" are price words ("at most"), not time words.
    low_time = re.sub(r"tối\s*(đa|thiểu)", "", low)
    if "buổi sáng" in low_time or re.search(r"\bsáng\b", low_time):
        out["time_of_day"] = (6, 11)
    elif "buổi chiều" in low_time or re.search(r"\bchiều\b", low_time):
        out["time_of_day"] = (12, 17)
    elif "buổi tối" in low_time or re.search(r"\btối\b", low_time):
        out["time_of_day"] = (17, 23)
    elif re.search(r"\btrưa\b", low_time):
        out["time_of_day"] = (11, 13)
    return out


def match_customer_profile(post: dict, profile: str) -> tuple[bool, dict]:
    """Check one post against a free-text customer profile.

    Returns (kept, details) with details = {"matched": [...],
    "contradicted": [...], "unknown": [...]}. A criterion is:
    - matched: the post states it and it agrees with the profile.
    - contradicted: the post states the opposite (wrong gender/level,
      price above the max, time outside the wanted part of day).
    - unknown: the post does not state it at all.
    A post is kept when it contradicts nothing and matches at least one
    stated criterion. Missing info is reported as unknown, never guessed
    and never a reason to drop the post on its own. No criteria in the
    profile -> kept.
    """
    criteria = parse_customer_profile(profile)
    if not criteria:
        return True, {"matched": ["no_criteria"], "contradicted": [], "unknown": []}
    content = norm_vn(post.get("content") or "")
    extracted = post.get("extracted") or {}
    matched: list[str] = []
    contradicted: list[str] = []
    unknown: list[str] = []
    if "levels" in criteria:
        have = {str(level).upper() for level in (extracted.get("level") or [])}
        if not have:
            unknown.append("level")
        elif {str(level).upper() for level in criteria["levels"]} & have:
            matched.append("level")
        else:
            contradicted.append("level")
    if "gender" in criteria:
        want = norm_vn(criteria["gender"])
        other = "nam" if want == "nu" else "nu"
        if re.search(rf"\b{re.escape(want)}\b", content):
            matched.append("gender")
        elif re.search(rf"\b{other}\b", content):
            contradicted.append("gender")
        else:
            unknown.append("gender")
    if "locations" in criteria:
        location = norm_vn(extracted.get("location") or "")
        if any(norm_vn(loc) in content or norm_vn(loc) in location
               for loc in criteria["locations"]):
            matched.append("location")
        else:
            unknown.append("location")
    price = parse_price_vnd(extracted.get("price"))
    if "price_max" in criteria and criteria["price_max"] is not None:
        if price is None:
            unknown.append("price_max")
        elif price <= criteria["price_max"]:
            matched.append("price_max")
        else:
            contradicted.append("price_max")
    if "price_min" in criteria and criteria["price_min"] is not None:
        if price is None:
            unknown.append("price_min")
        elif price >= criteria["price_min"]:
            matched.append("price_min")
        else:
            contradicted.append("price_min")
    if "time_of_day" in criteria:
        hour = None
        t = extracted.get("time") or ""
        m = re.search(r"(\d{1,2})h", str(t))
        if m:
            hour = int(m.group(1))
        else:
            m = re.search(r"(\d{1,2}):\d{2}", str(t))
            if m:
                hour = int(m.group(1))
        start, end = criteria["time_of_day"]
        if hour is not None:
            if start <= hour < end or (end == 23 and hour == 23):
                matched.append("time_of_day")
            else:
                contradicted.append("time_of_day")
        else:
            # No clock time: fall back to part-of-day words ("tối nay",
            # "sáng mai"). "tối đa" (maximum price) is not a time word.
            words = re.sub(r"toi\s*(da|thieu)", "", content)
            word_ranges = {"sang": (6, 11), "trua": (11, 13),
                           "chieu": (12, 17), "toi": (17, 23)}
            found = [rng for word, rng in word_ranges.items()
                     if re.search(rf"\b{word}\b", words)]
            if not found:
                unknown.append("time_of_day")
            elif any(rng == (start, end) for rng in found):
                matched.append("time_of_day")
            else:
                contradicted.append("time_of_day")
    details = {"matched": matched, "contradicted": contradicted, "unknown": unknown}
    kept = not contradicted and bool(matched)
    return kept, details


def post_matches_filters(post: dict, task: dict) -> bool:
    """Extra filters, all optional. A filter only applies when the task sets it.

    - levels: list like ["TB+", "TBK"] — post must have one of these levels.
    - min_price / max_price: VND numbers — post must have a price in range.
    - location_keyword: text that must appear in the content or location.
    - exclude_keywords: list — post is dropped if any appears in content.
    - customer_profile: free text from the orchestrator describing the
      target customer, e.g. "Nữ chơi cầu lông trình TB+, ở Hà Nội, cần
      sân buổi tối, giá dưới 100k". A post is kept when it contradicts
      none of the criteria pulled from the text (level, gender, location,
      price, time of day) and matches at least one of them; criteria the
      post does not state are reported as unknown, not a reason to drop.
    Keyword checks are accent-insensitive ("tuyen" matches "tuyển").
    A post that lacks the info a fixed filter needs is dropped, never guessed.
    """
    content = (post.get("content") or "").lower()
    extracted = post.get("extracted") or {}
    levels = task.get("levels")
    if levels:
        wanted = {str(level).upper() for level in levels}
        have = {str(level).upper() for level in (extracted.get("level") or [])}
        if not (wanted & have):
            return False
    min_price = task.get("min_price")
    max_price = task.get("max_price")
    if min_price is not None or max_price is not None:
        price = parse_price_vnd(extracted.get("price"))
        if price is None:
            return False
        if min_price is not None and price < int(min_price):
            return False
        if max_price is not None and price > int(max_price):
            return False
    location_keyword = (task.get("location_keyword") or "").strip()
    if location_keyword:
        location = str(extracted.get("location") or "")
        if not contains_vn(content, location_keyword) and not contains_vn(location, location_keyword):
            return False
    for word in task.get("exclude_keywords") or []:
        if str(word).strip() and contains_vn(content, str(word).strip()):
            return False
    profile = (task.get("customer_profile") or "").strip()
    if profile:
        matched, details = match_customer_profile(post, profile)
        if not matched:
            return False
        post["profile_match"] = details
    return True


# ------------------------------------------------------- Facebook Graph API

class FacebookGraph:
    """Minimal Graph API client. Only what AGENT-V1 needs, nothing more."""

    def __init__(self, token: str):
        if requests is None:
            raise RuntimeError("Missing dependency: pip install -r requirements.txt")
        if not token:
            raise RuntimeError("Missing FB_ACCESS_TOKEN. Copy .env.example to .env and fill it in.")
        self.token = token
        self.base = f"https://graph.facebook.com/{VERSION}"

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "access_token": self.token}
        resp = requests.get(f"{self.base}/{path}", params=params, timeout=30)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            raise RuntimeError(f"Facebook API error {resp.status_code}: {err.get('message', resp.text[:300])}")
        return data

    def group_details(self, group_id: str) -> dict:
        return self._get(group_id, {"fields": "id,name,description,member_count"})

    def search_groups(self, keywords: str, limit: int) -> list[dict]:
        # Note: Graph API group search is restricted. If Meta refuses it,
        # this raises a clear error instead of returning fake groups.
        data = self._get("search", {"q": keywords, "type": "group", "limit": limit})
        return data.get("data", [])

    def group_posts(self, group_id: str, limit: int) -> list[dict]:
        data = self._get(
            f"{group_id}/feed",
            {"fields": "id,message,created_time,permalink_url,likes.summary(true),comments.summary(true)",
             "limit": limit},
        )
        return data.get("data", [])


def normalise_post(raw: dict, wanted: list[str]) -> dict:
    content = raw.get("message", "") or ""
    likes = raw.get("likes", {}).get("summary", {}).get("total_count", 0)
    comments = raw.get("comments", {}).get("summary", {}).get("total_count", 0)
    return {
        "post_id": raw.get("id", ""),
        "post_url": raw.get("permalink_url", ""),
        "created_at": raw.get("created_time", ""),
        "content": content,
        "react_count": likes,
        "comment_count": comments,
        "extracted": extract_fields(content, wanted),
    }


# ---------------------------------------------------------------- workflow

def run_task(task: dict, fb: FacebookGraph | None, demo: bool) -> dict:
    task_id = task.get("task_id", "")
    action = task.get("action", "")
    limit = int(task.get("limit", 10) or 10)
    limit = max(1, min(limit, 20 if action == "search_group_posts" else 25))
    wanted = task.get("fields_to_extract", []) or []

    if demo:
        sample = load_json(Path(__file__).parent / "example-result.json")
        sample["task_id"] = task_id or sample.get("task_id", "TASK-DEMO")
        sample["status"] = "success"
        # Date filter also applies in demo mode, so you can test it offline.
        date_from = task.get("date_from")
        date_to = task.get("date_to")
        kept = [p for p in sample.get("posts", [])
                if in_date_range(p.get("created_at", ""), date_from, date_to)]
        sample["posts"] = kept
        sample["summary"] = (
            f"Demo mode: replayed bundled example result, no Facebook call was made. "
            f"Date filter {date_from or '...'} to {date_to or '...'} kept {len(kept)} post(s)."
        )
        return sample

    if not task_id or not action:
        return {"task_id": task_id, "status": "error",
                "errors": ["Missing required field: task_id and action are required."],
                "posts": [], "groups": [], "summary": "Task rejected: missing task_id or action."}

    if fb is None:
        return {"task_id": task_id, "status": "error",
                "errors": ["Missing FB_ACCESS_TOKEN. See README-STANDALONE.md."],
                "posts": [], "groups": [], "summary": "Task rejected: no Facebook token."}

    try:
        if action == "find_groups":
            keywords = task.get("group_keywords", "")
            if not keywords:
                return {"task_id": task_id, "status": "error",
                        "errors": ["Missing required field: group_keywords."],
                        "posts": [], "groups": [], "summary": "Task rejected: missing group_keywords."}
            groups = fb.search_groups(keywords, limit)
            out = [{"id": g.get("id", ""), "name": g.get("name", ""),
                    "description": g.get("description", ""), "member_count": g.get("member_count"),
                    "url": f"https://www.facebook.com/groups/{g.get('id', '')}/"} for g in groups]
            return {"task_id": task_id, "status": "success", "errors": [], "groups": out, "posts": [],
                    "summary": f"Found {len(out)} groups for keyword '{keywords}'."}

        if action == "search_group_posts":
            group_id = task.get("group_id") or task.get("group_name", "")
            if not group_id:
                return {"task_id": task_id, "status": "error",
                        "errors": ["Missing required field: group_id or group_name."],
                        "posts": [], "groups": [], "summary": "Task rejected: missing group_id or group_name."}
            group = fb.group_details(group_id)
            keyword = (task.get("post_keyword") or "").strip()
            date_from = task.get("date_from")
            date_to = task.get("date_to")
            # Fetch extra posts: keyword + date filtering happen locally,
            # so some fetched posts will be dropped before reaching `limit`.
            raw_posts = fb.group_posts(group["id"], limit * 3)
            posts = []
            for raw in raw_posts:
                if not in_date_range(raw.get("created_time", ""), date_from, date_to):
                    continue
                content = (raw.get("message") or "")
                if keyword and not contains_vn(content, keyword):
                    continue
                post = normalise_post(raw, wanted)
                if not post_matches_filters(post, task):
                    continue
                posts.append(post)
                if len(posts) >= limit:
                    break
            date_note = ""
            if date_from or date_to:
                date_note = f" from {date_from or '...'} to {date_to or '...'}"
            return {"task_id": task_id, "status": "success", "errors": [],
                    "group": {"id": group.get("id", ""), "name": group.get("name", ""),
                              "member_count": group.get("member_count"),
                              "url": f"https://www.facebook.com/groups/{group.get('id', '')}/",
                              "visibility": group.get("privacy", "")},
                    "posts": posts, "groups": [],
                    "summary": f"Found {len(posts)} posts in group '{group.get('name', '')}'{date_note}."}

        return {"task_id": task_id, "status": "error",
                "errors": [f"Unknown action: {action}. Use search_group_posts or find_groups."],
                "posts": [], "groups": [], "summary": f"Task rejected: unknown action '{action}'."}
    except Exception as exc:  # noqa: BLE001 - worker must return JSON, not crash
        return {"task_id": task_id, "status": "error", "errors": [str(exc)],
                "posts": [], "groups": [], "summary": f"Task failed: {exc}"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AGENT-V1 standalone worker")
    parser.add_argument("--task", default="example-task.json", help="Input task JSON file")
    parser.add_argument("--out", default="result.json", help="Output result JSON file")
    parser.add_argument("--demo", action="store_true", help="Replay bundled example, no Facebook call")
    args = parser.parse_args(argv)

    root = Path(__file__).parent
    load_dotenv(root / ".env")

    task_path = Path(args.task)
    if not task_path.exists():
        print(f"Task file not found: {task_path}", file=sys.stderr)
        return 2
    task = load_json(task_path)

    fb = None
    if not args.demo:
        token = os.environ.get("FB_ACCESS_TOKEN", "")
        try:
            fb = FacebookGraph(token)
        except RuntimeError as exc:
            result = {"task_id": task.get("task_id", ""), "status": "error",
                      "errors": [str(exc)], "posts": [], "groups": [],
                      "summary": f"Task rejected: {exc}"}
            save_json(Path(args.out), result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1

    result = run_task(task, fb, demo=args.demo)
    save_json(Path(args.out), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
