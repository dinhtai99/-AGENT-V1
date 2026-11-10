"""AGENT-V1 — shared post filtering and extraction.

Pure functions only, no Facebook calls: reading post text, extracting
fields, matching task filters and customer profiles. Used by
browser_worker.py. Split out of the old token worker (worker.py, deleted
2026-11-10) so the browser worker keeps working without the dead
Groups API code.
"""

import json
import re
import unicodedata
from datetime import date
from pathlib import Path


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
