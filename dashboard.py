#!/usr/bin/env python3
"""AGENT-V1 — Build the lead dashboard (dashboard.html).

Reads everything the browser worker has saved so far:
- leads.json   cumulative small-business owner list
- result.json  last run (full post text, per-group stats)
- debug.json   every raw post read in the last --debug run
- debug-groups.json  names of the Facebook groups you have joined

All posts are re-analysed with the current rules in filters.py, merged and
de-duplicated, then embedded into one self-contained dashboard.html that
opens straight from disk (no server, no internet needed).

Usage:
    python3 dashboard.py            # rebuild leads.json/leads.csv + dashboard.html
    python3 dashboard.py --open     # ... and open it in the browser
    python3 dashboard.py --no-save  # only write dashboard.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from filters import (  # noqa: E402
    analyze_business_and_needs,
    analyze_lead_behavior,
    build_leads_list,
    clean_fb_noise,
    extract_fields,
    load_json,
    norm_vn,
    parse_customer_profile,
    post_matches_filters,
    save_json,
    save_leads_csv,
    score_lead_web_priority,
)

TEMPLATE_NAME = "dashboard_template.html"
OUTPUT_NAME = "dashboard.html"
PLACEHOLDER = "/*__DASHBOARD_DATA__*/"

# Fallback names for the groups in the default task (used when Facebook
# did not give us a clean name).
DEFAULT_GROUP_NAMES = {
    "nhatbanchotoinhe": "Nhật Bản chờ tôi nhé 🇯🇵",
    "kinhdoanhtainhat": "Kinh Doanh Tại Nhật Group",
    "quanviettainhat": "Cộng đồng Quán Việt tại Nhật",
    "1418096345848212": "Hội Nail Người Việt Tại Nhật 💅🇯🇵",
    "475272478129435": "Cộng đồng nail tại Nhật",
    "730082261664690": "Nail xinh tại Nhật",
    "292965717052020": "Hội nail tại Nhật",
    "hoilamdeptainhat": "Cộng đồng Spa, Nail, Makeup tại Nhật",
    "1454607818379390": "HỘI MẪU MAKEUP, NAIL,MI TOKYO",
    "1698901880986426": "Mẫu Nail mi make osaka japan",
    "landaumotiemnail": "Lần đầu mở tiệm nail",
    "1516948868554215": "Tokyo baito",
    "453070261457981": "Nagoya Baito",
    "nagoyabaito": "NAGOYA BAITO RAO VẶT",
    "222598458270175": "Aichi baito",
}

NAME_PLACEHOLDERS = {"", "chua ro ten fb", "thanh vien an danh", "anonymous participant"}

# business_type (from filters.BUSINESS_CATEGORIES) -> (short label, key)
SECTORS = [
    ("Nhà hàng / Quán ăn Việt Nam", "Quán ăn Việt", "fnb"),
    ("Quán nhậu / Izakaya", "Izakaya / Quán nướng", "fnb"),
    ("Quán Cafe", "Cafe / Tiệm bánh", "fnb"),
    ("Quán ăn / Nhà hàng (F&B)", "Quán ăn / Nhà hàng", "fnb"),
    ("Xưởng chế biến", "Bento / Chế biến", "food"),
    ("Kinh doanh Thực phẩm", "Thực phẩm / Hoa quả", "food"),
    ("Tiệm Nail", "Nail / Spa / Làm đẹp", "beauty"),
    ("Shop bán hàng", "Shop online / Kho hàng", "shop"),
    ("Kinh doanh Online", "Bán hàng cá nhân", "shop"),
    ("Cửa hàng Đồ cũ", "Đồ cũ / Recycle", "recycle"),
    ("Dịch vụ Viễn thông", "Wifi / Sim / Internet", "telecom"),
    ("Đại lý Vé máy bay", "Vé máy bay / Du lịch", "travel"),
    ("Bất động sản", "Nhà ở / Bất động sản", "realestate"),
    ("Dịch vụ Vận chuyển", "Vận chuyển / Dịch vụ", "logistics"),
]

OFFERS = {
    "fnb": "Thu hút khách đặt bàn (đăng bài nhóm định kỳ, menu/QR order, quản lý đặt bàn), hỗ trợ tuyển baito khi đông khách.",
    "food": "Kênh bán online + chốt đơn tự động qua inbox, quản lý đơn/ship, đẩy hàng theo mùa nhanh hơn.",
    "shop": "Chốt đơn & quản lý đơn tự động, đăng bài nhiều nhóm cùng lúc, catalog sản phẩm gọn để khách xem.",
    "recycle": "Đăng tin tự động lên nhiều nhóm, catalog đồ cũ có giá, quản lý lịch giao/nhận đồ.",
    "telecom": "Tiếp cận người Việt mới sang Nhật, chatbot tư vấn gói cước, theo dõi khách đã đăng ký.",
    "travel": "Chatbot báo giá vé nhanh, danh sách khách đặt vé, nhắc khách quay lại mùa Tết/hè.",
    "realestate": "Chuẩn hoá tin đăng nhà, lọc khách thuê theo khu vực/ngân sách, CRM khách đang tìm nhà.",
    "beauty": "Đặt lịch online, nhắc lịch khách, tuyển thợ/học viên.",
    "logistics": "Nhận đơn & xếp lịch tự động, tiếp cận khách qua các nhóm khu vực.",
    "other": "Hỏi thêm về mô hình qua inbox để xác định đúng nhu cầu.",
}

ERROR_HINTS = [
    ("login", "Phiên đăng nhập Facebook hết hạn. Chạy lại: python browser_worker.py --login"),
    ("checkpoint", "Facebook yêu cầu xác minh. Đăng nhập lại bằng --login."),
    ("timeout", "Facebook tải quá chậm. Thử quét lại sau ít phút."),
    ("no posts", "Không đọc được bài nào — có thể nhóm cần duyệt thành viên, hoặc Facebook đổi giao diện. Chạy lại với --debug."),
]


# ---------------------------------------------------------------- helpers

def _read(path: Path):
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception:
        return None


def _name_like(text: str | None) -> bool:
    s = (text or "").strip()
    return bool(s) and len(s) <= 40 and len(s.split()) <= 5 and norm_vn(s) not in NAME_PLACEHOLDERS


def _is_real_name(text: str | None) -> bool:
    return norm_vn((text or "").strip()) not in NAME_PLACEHOLDERS


def _post_url(pid: str, gid: str) -> str:
    if pid and pid.isdigit() and gid:
        return f"https://www.facebook.com/groups/{gid}/posts/{pid}/"
    return f"https://www.facebook.com/groups/{gid}/" if gid else ""


def _sector(business_type: str | None) -> tuple[str, str]:
    bt = business_type or ""
    for prefix, label, key in SECTORS:
        if bt.startswith(prefix):
            return label, key
    return "Kinh doanh nhỏ khác", "other"


def _headline(content: str) -> str:
    for ln in (content or "").splitlines():
        s = ln.strip(" -–—•*#")
        if len(s) >= 12 and not re.match(r"^(?:khoảng\s+)?\d", s.lower()):
            return s if len(s) <= 110 else s[:107].rstrip() + "…"
    return ""


def _group_names(root: Path, result: dict | None) -> dict[str, str]:
    names = dict(DEFAULT_GROUP_NAMES)
    disc = _read(root / "discovered_groups.json")
    if isinstance(disc, dict):
        for g in disc.get("groups") or []:
            if g.get("id") and g.get("name"):
                names[str(g["id"])] = g["name"]
    dg = _read(root / "debug-groups.json")
    if isinstance(dg, dict):
        for g in dg.get("groups") or []:
            if g.get("id") and g.get("name") and "chưa đọc" not in g["name"].lower():
                names[str(g["id"])] = g["name"]
    for g in (result or {}).get("groups") or []:
        nm = (g.get("name") or "").strip()
        if g.get("id") and nm and "chưa đọc" not in nm.lower() and "bình luận trong" not in nm.lower():
            names[str(g["id"])] = nm
    return names


# ---------------------------------------------------------------- collect & analyse

def collect_posts(root: Path) -> tuple[dict[str, dict], dict[str, dict], dict | None]:
    """Merge every post we know about, keyed by post_id.

    Returns (posts, old_personal_info_by_post_id, result_json).
    """
    leads_doc = _read(root / "leads.json")
    result = _read(root / "result.json")
    debug = _read(root / "debug.json")
    posts: dict[str, dict] = {}
    old_info: dict[str, dict] = {}

    def upsert(pid: str, **fields) -> None:
        if not pid:
            return
        p = posts.setdefault(pid, {
            "post_id": pid, "post_url": "", "author_name": "", "author_url": "", "group_member_url": "",
            "group_id": "", "created_at": "", "content": "", "debug_category": "", "debug_reason": "",
        })
        content = fields.pop("content", "") or ""
        if len(content) > len(p["content"]):
            p["content"] = content
        for key, val in fields.items():
            if val and not p.get(key):
                p[key] = val

    old_leads = []
    if isinstance(leads_doc, dict):
        old_leads = leads_doc.get("leads") or []
    elif isinstance(leads_doc, list):
        old_leads = leads_doc
    for lead in old_leads:
        info = lead.get("personal_info") or {}
        for sp in lead.get("source_posts") or []:
            pid = str(sp.get("post_id") or "")
            old_info[pid] = info
            upsert(
                pid,
                post_url=sp.get("post_url"),
                group_id=sp.get("group_id"),
                created_at=sp.get("created_at"),
                content=sp.get("content") or sp.get("content_snippet"),
                author_name=info.get("author_name") if _is_real_name(info.get("author_name")) else "",
                author_url=info.get("author_url"),
                group_member_url=info.get("group_member_url"),
            )

    for p in (result or {}).get("posts") or []:
        upsert(
            str(p.get("post_id") or ""),
            post_url=p.get("post_url"), group_id=p.get("group_id"), created_at=p.get("created_at"),
            content=p.get("content"), author_name=p.get("author_name"), author_url=p.get("author_url"),
            group_member_url=p.get("group_member_url"),
        )

    for gid, items in ((debug or {}).get("groups_raw_posts") or {}).items():
        for p in items or []:
            pid = str(p.get("post_id") or "")
            upsert(
                pid, group_id=gid, author_name=p.get("author_name"), author_url=p.get("author_url"),
                content=p.get("content_preview"), debug_category=p.get("category"), debug_reason=p.get("reason"),
            )

    # Older scans used a post's first line as the "author" when no profile link
    # was found. Put such sentences back into the text so they get analysed.
    for p in posts.values():
        name = (p.get("author_name") or "").strip()
        if name and not _name_like(name):
            if not p["content"].startswith(name):
                p["content"] = f"{name}\n{p['content']}"
            p["author_name"] = ""
        p["content"] = clean_fb_noise(p["content"], author_name=p.get("author_name") or "")
        if not p.get("post_url"):
            p["post_url"] = _post_url(p["post_id"], p.get("group_id") or "")
    return posts, old_info, result


# ---------------------------------------------------------------- lead presentation

def _display_name(lead: dict, posts: list[dict], sector_label: str, area: str) -> str:
    b = lead.get("step1_business_model") or {}
    p = lead.get("personal_info") or {}
    if _name_like(b.get("shop_name")):
        return b["shop_name"].strip()
    if _name_like(p.get("author_name")):
        return p["author_name"].strip()
    text = "\n".join(sp.get("content") or "" for sp in posts)
    # "678Quán•Ga imaike" style brand tokens (digits glued to a word)
    m = re.search(r"\b(\d+[A-Za-zÀ-ỹ]{2,})\b", text)
    if m:
        return m.group(1)
    # Signature lines such as "WIFI JP – MẠNH PHẠM"
    for ln in reversed(text.splitlines()):
        s = ln.strip()
        if 4 <= len(s) <= 40 and re.search(r"\s[–—-]\s", s) and s.upper() == s:
            return s
    # Latin all-caps brand of 2+ words, e.g. "JAPAN TRAVEL"
    m = re.search(r"\b([A-Z]{4,}(?:\s+[A-Z]{4,})+)\b", text)
    if m:
        return m.group(1)
    tag = re.search(r"#([A-Za-zÀ-ỹ]{3,20})\b", text)
    if tag and _sector(b.get("business_type"))[1] == "realestate":
        return f"Nhà cho thuê #{tag.group(1)}"
    return f"{sector_label} · {area}" if area else sector_label


def _clean_area(b: dict) -> tuple[str, str]:
    areas = [a for a in (b.get("areas") or []) if "(theo nhóm)" not in a]
    group_areas = [a.replace(" (theo nhóm)", "") for a in (b.get("areas") or []) if "(theo nhóm)" in a]
    stations = [s for s in (b.get("stations") or []) if s]
    station = re.sub(r"\s+\d+\s*p$", "", stations[0]).strip().title() if stations else ""
    main = areas[0] if areas else (group_areas[0] if group_areas else "")
    label_parts = [x for x in (main, f"Ga {station}" if station else "") if x]
    return " · ".join(label_parts), main or "Chưa rõ"


def present_lead(lead: dict, post_map: dict[str, dict], group_names: dict[str, str]) -> dict:
    p = lead.get("personal_info") or {}
    b = lead.get("step1_business_model") or {}
    n = lead.get("step2_deep_needs") or {}
    sector_label, sector_key = _sector(b.get("business_type"))
    area, area_key = _clean_area(b)

    posts = []
    group_ids: list[str] = []
    for sp in lead.get("source_posts") or []:
        full = post_map.get(str(sp.get("post_id") or "")) or {}
        gid = str(sp.get("group_id") or full.get("group_id") or "")
        if gid and gid not in group_ids:
            group_ids.append(gid)
        created_val = sp.get("created_at") or full.get("created_at") or "Đợt quét 1 năm"
        posts.append({
            "post_id": sp.get("post_id"),
            "url": sp.get("post_url") or full.get("post_url") or "",
            "group_name": group_names.get(gid, gid),
            "created_at": created_val,
            "content": full.get("content") or sp.get("content_snippet") or "",
        })

    phones = list(dict.fromkeys(p.get("all_phones") or ([p["phone"]] if p.get("phone") else [])))
    has_phone = bool(phones or p.get("zalo_id") or p.get("line_id"))
    has_fb = bool(p.get("author_url"))
    activity = "hiring" if b.get("owner_activity") == "owner_hiring" else "selling"
    price = n.get("wage_offered") or ""

    score = 0
    score += 3 if has_phone else 0
    score += 3 if has_fb else 0
    score += 1 if p.get("group_member_url") else 0
    score += 1 if area else 0
    score += 1 if price else 0
    score += min(len(posts) - 1, 2)
    priority = "high" if score >= 5 else "mid" if score >= 3 else "low"

    name = _display_name(lead, posts, sector_label, area_key if area_key != "Chưa rõ" else "")
    headline = _headline(posts[0]["content"]) if posts else ""
    what = b.get("business_type") or sector_label
    if headline:
        what = f"{what} — “{headline}”"

    group_label = ", ".join(group_names.get(g, g) for g in group_ids[:1]) or "nhóm Baito"
    opener = (
        f"Chào anh/chị{' ' + p['author_name'] if _name_like(p.get('author_name')) else ''}, "
        f"em thấy bài đăng của mình trong nhóm {group_label} "
        f"({sector_label.lower()}{' ở ' + area_key if area_key != 'Chưa rõ' else ''}). "
        f"Bên em đang hỗ trợ các chủ kinh doanh nhỏ người Việt tại Nhật: {OFFERS[sector_key].rstrip('.').lower()}. "
        "Anh/chị có tiện trao đổi 5 phút không ạ?"
    )

    key = lead.get("lead_key") or name
    beh = analyze_lead_behavior(posts, lead)
    maps_info = lead.get("maps_info") or {}
    wp = score_lead_web_priority(lead, beh, maps_info)
    final_opener = wp["web_opener"] if maps_info.get("status") in ("FOUND", "NOT_FOUND") else opener
    priority = "high" if wp["score"] >= 7 else "mid" if wp["score"] >= 5 else "low"
    ready_for_maps = bool(
        b.get("shop_name")
        or beh.get("shop_names")
        or phones
        or beh.get("phones")
        or any(re.search(r"(?:〒|\d+-\d+-\d+|[都道府県])", a) for a in (beh.get("address_clues") or []))
    )

    return {
        "id": "L" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10],
        "lead_key": key,
        "name": name,
        "subtitle": headline,
        "author_name": p.get("author_name") if _is_real_name(p.get("author_name")) else "",
        "fb_url": p.get("author_url") or "",
        "member_url": p.get("group_member_url") or "",
        "phones": phones,
        "zalo": p.get("zalo_id") or "",
        "line": p.get("line_id") or "",
        "contact_methods": p.get("contact_methods") or [],
        "has_phone": has_phone,
        "has_fb": has_fb,
        "business_type": b.get("business_type") or "",
        "shop_name": b.get("shop_name") or (beh.get("shop_names") or [""])[0] or "",
        "sector": sector_label,
        "sector_key": sector_key,
        "activity": activity,
        "activity_label": "Đang tuyển người" if activity == "hiring" else "Đang bán hàng / dịch vụ",
        "what_they_do": what,
        "scale": b.get("scale_estimate") or "",
        "area": area,
        "area_key": area_key,
        "stations": b.get("stations") or [],
        "price": price,
        "segment": n.get("shop_customer_segment") or "",
        "roles": n.get("roles_needed") or [],
        "shifts": n.get("shifts_needed") or [],
        "benefits": n.get("benefits_offered") or [],
        "needs": n.get("pain_points_and_needs") or [],
        "group_ids": group_ids,
        "group_names": [group_names.get(g, g) for g in group_ids],
        "posts": posts,
        "posted_at": (posts[0]["created_at"] if posts else "Đợt quét 1 năm"),
        "score": score,
        "score_10": wp["score"],
        "score_tier": wp["tier"],
        "score_preliminary": wp["is_preliminary"],
        "score_reasons": wp["reasons"],
        "behavior": beh,
        "maps": maps_info,
        "ready_for_maps": ready_for_maps,
        "deep_profiled_at": lead.get("deep_profiled_at") or "",
        "priority": priority,
        "approach": {"offer": OFFERS[sector_key], "opener": final_opener, "web_opener": wp["web_opener"]},
    }


CATEGORY_LABELS = {
    "small_business_owner": "Giữ lại — chủ KD nhỏ",
    "job_seeker": "Loại — người tìm việc",
    "agency_or_large": "Loại — môi giới / công ty lớn",
    "unknown": "Loại — không phải bài kinh doanh",
}


# ---------------------------------------------------------------- build

def build(root: Path) -> tuple[dict, list[dict]]:
    """Return (dashboard_data, rebuilt_raw_leads)."""
    posts, old_info, result = collect_posts(root)
    group_names = _group_names(root, result)
    task = _read(root / "task-baito-nhom-da-tham-gia.json") or {}
    if not isinstance(task, dict):
        task = {}

    targets: list[dict] = []
    raw_rows: list[dict] = []
    for p in posts.values():
        gid = p.get("group_id") or ""
        p["group_name"] = group_names.get(gid, gid)
        analysis = analyze_business_and_needs(
            p["content"], author_name=p.get("author_name") or "", group_name=p["group_name"], group_id=gid
        )
        p["business_analysis"] = analysis
        info = old_info.get(p["post_id"]) or {}
        # Keep contacts found by earlier scans (their snippets may have been cut)
        contact = analysis["contact"]
        for ph in info.get("all_phones") or []:
            if ph not in contact["all_phones"]:
                contact["all_phones"].append(ph)
        contact["phone"] = contact["phone"] or info.get("phone")
        contact["zalo_id"] = contact["zalo_id"] or info.get("zalo_id")
        contact["line_id"] = contact["line_id"] or info.get("line_id")
        wanted = set(task.get("fields_to_extract") or []) | {"location", "level", "time", "price", "wage"}
        p["extracted"] = extract_fields(
            p["content"], sorted(wanted), author_name=p.get("author_name") or "",
            group_name=p["group_name"], group_id=gid,
        )
        is_lead = post_matches_filters(p, task)
        if is_lead:
            targets.append(p)
        if not p["content"] and not p.get("author_name"):
            continue
        raw_rows.append({
            "post_id": p["post_id"],
            "url": p.get("post_url") or "",
            "group_name": p["group_name"],
            "author": p.get("author_name") or "",
            "created_at": p.get("created_at") or "Đợt quét 1 năm",
            "is_lead": is_lead,
            "category_label": ("Loại — không khớp khách hàng mục tiêu" if analysis["is_target_owner"] and not is_lead
                               else CATEGORY_LABELS.get(analysis["post_category"], analysis["post_category"])),
            "reason": (", ".join((p.get("profile_match") or {}).get("contradicted", []) +
                                  (p.get("profile_match") or {}).get("unknown", [])) if analysis["is_target_owner"] and not is_lead
                       else analysis["classification_reason"]),
            "content": p["content"],
        })

    leads = build_leads_list(targets)
    saved = _read(root / "leads.json") or {}
    saved_leads = saved.get("leads", []) if isinstance(saved, dict) else saved
    saved_updated = saved.get("updated_at") or time.strftime("%Y-%m-%d %H:%M:%S")
    saved_by_key = {lead.get("lead_key"): lead for lead in saved_leads if isinstance(lead, dict) and lead.get("lead_key")}
    for lead in leads:
        old_l = saved_by_key.get(lead.get("lead_key"))
        if old_l:
            for extra_k in ("crm", "maps_info", "behavior_analysis", "web_priority", "deep_profiled_at"):
                if old_l.get(extra_k):
                    lead[extra_k] = old_l[extra_k]
    post_map = {p["post_id"]: p for p in posts.values()}
    presented = [present_lead(lead, post_map, group_names) for lead in leads]
    for item in presented:
        old_l = saved_by_key.get(item["lead_key"]) or {}
        item["crm"] = old_l.get("crm") or {}
        item["updated_at"] = saved_updated
        item["scanned_at"] = saved_updated
    presented.sort(key=lambda x: (-x.get("score_10", 0), -x["score"], x["name"].lower()))
    raw_rows.sort(key=lambda r: (not r["is_lead"], r["group_name"]))

    disc_file = root / "discovered_groups.json"
    discovered_data = load_json(disc_file) if disc_file.exists() else {}
    disc_map = {str(dg.get("id")): dg for dg in (discovered_data.get("groups") or []) if dg.get("id")}

    groups_out: list[dict] = []
    seen_gids: set[str] = set()
    for g in (result or {}).get("groups") or []:
        gid = str(g.get("id") or "")
        seen_gids.add(gid)
        err = g.get("error") or ""
        hint = next((h for k, h in ERROR_HINTS if k in err.lower()), "Xem lỗi chi tiết trong result.json.") if err else ""
        dg_info = disc_map.get(gid) or {}
        groups_out.append({
            "id": gid,
            "name": group_names.get(gid, gid),
            "url": g.get("url") or f"https://www.facebook.com/groups/{gid}/",
            "raw_posts_read": g.get("raw_posts_read", 0) or sum(1 for r in raw_rows if r["group_name"] == group_names.get(gid, gid)),
            "leads_found": sum(1 for l in presented if gid in l["group_ids"]),
            "error": err,
            "error_hint": hint,
            "member_text": dg_info.get("member_text") or "",
        })
    for gid in sorted({p.get("group_id") for p in posts.values() if p.get("group_id")} - seen_gids):
        seen_gids.add(gid)
        dg_info = disc_map.get(gid) or {}
        groups_out.append({
            "id": gid, "name": group_names.get(gid, gid), "url": f"https://www.facebook.com/groups/{gid}/",
            "raw_posts_read": sum(1 for p in posts.values() if p.get("group_id") == gid),
            "leads_found": sum(1 for l in presented if gid in l["group_ids"]), "error": "", "error_hint": "",
            "member_text": dg_info.get("member_text") or "",
        })

    # Include newly discovered & target groups that haven't been scanned yet
    target_gids = [str(x).strip() for x in (task.get("group_ids") or []) if str(x).strip()]
    for gid in list(disc_map.keys()) + target_gids:
        if gid and gid not in seen_gids:
            seen_gids.add(gid)
            dg_info = disc_map.get(gid) or {}
            g_name = dg_info.get("name") or group_names.get(gid, gid)
            groups_out.append({
                "id": gid,
                "name": g_name,
                "url": dg_info.get("url") or f"https://www.facebook.com/groups/{gid}/",
                "raw_posts_read": 0,
                "leads_found": 0,
                "error": "Chờ quét 1 năm",
                "error_hint": "Nhóm kinh doanh mới khám phá (>5k tv). Bấm 'Quét sâu 1 năm' để trích xuất bài viết.",
                "member_text": dg_info.get("member_text") or "",
            })

    result_mtime = (root / "result.json").stat().st_mtime if (root / "result.json").exists() else time.time()
    data = {
        "meta": {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(result_mtime)),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_raw_posts": len(raw_rows),
            "customer_profile": task.get("customer_profile") or "",
            "profile_criteria": parse_customer_profile(task.get("customer_profile") or ""),
            "summary": (result or {}).get("summary", ""),
        },
        "leads": presented,
        "raw_posts": raw_rows,
        "groups": groups_out,
        "discovered_groups": discovered_data,
    }
    return data, leads


def render_html(root: Path, data: dict) -> str:
    template = (root / TEMPLATE_NAME).read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False)
    # Never let post text close the <script> tag early
    payload = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    return template.replace(PLACEHOLDER, payload)


def generate(root: Path | None = None, save_leads: bool = True) -> Path:
    """Rebuild data and write dashboard.html. Returns the output path."""
    root = Path(root or Path(__file__).parent)
    data, leads = build(root)
    if save_leads:
        leads_json = root / "leads.json"
        previous = _read(leads_json)
        old_leads = previous.get("leads", []) if isinstance(previous, dict) else previous if isinstance(previous, list) else []
        by_key = {lead["lead_key"]: lead for lead in old_leads if isinstance(lead, dict) and lead.get("lead_key")}
        for lead in leads:
            old = by_key.get(lead["lead_key"])
            if old:
                known_posts = {str(p.get("post_id") or p.get("post_url") or "") for p in lead.get("source_posts") or []}
                for source in old.get("source_posts") or []:
                    key = str(source.get("post_id") or source.get("post_url") or "")
                    if key and key not in known_posts:
                        lead["source_posts"].append(source)
                        known_posts.add(key)
                if old.get("crm") and not lead.get("crm"):
                    lead["crm"] = old["crm"]
                for extra_k in ("maps_info", "behavior_analysis", "web_priority", "deep_profiled_at"):
                    if old.get(extra_k) and not lead.get(extra_k):
                        lead[extra_k] = old[extra_k]
            by_key[lead["lead_key"]] = lead
        archived_leads = list(by_key.values())
        if leads_json.exists():
            shutil.copyfile(leads_json, root / "leads.prev.json")
        save_json(leads_json, {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_leads": len(archived_leads),
            "leads": archived_leads,
        })
        save_leads_csv(root / "leads.csv", archived_leads)
    out = root / OUTPUT_NAME
    content = render_html(root, data)
    out.write_text(content, encoding="utf-8")
    dist_dir = root / "dist"
    try:
        dist_dir.mkdir(exist_ok=True)
        index_file = dist_dir / "index.html"
        index_file.write_text(content, encoding="utf-8")

        headers_file = dist_dir / "_headers"
        headers_file.write_text("/*\n  Content-Type: text/html; charset=UTF-8\n  X-Frame-Options: SAMEORIGIN\n  X-Content-Type-Options: nosniff\n", encoding="utf-8")

        redirects_file = dist_dir / "_redirects"
        redirects_file.write_text("/*    /index.html   200\n", encoding="utf-8")

        import zipfile
        zip_path = root / "dist.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(index_file, arcname="index.html")
            zf.write(headers_file, arcname="_headers")
            zf.write(redirects_file, arcname="_redirects")
    except Exception:
        pass
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build dashboard.html from the scanned Baito leads")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in your browser")
    parser.add_argument("--no-save", action="store_true", help="Do not rewrite leads.json / leads.csv")
    args = parser.parse_args(argv)
    root = Path(__file__).parent
    out = generate(root, save_leads=not args.no_save)
    data = json.loads(
        out.read_text(encoding="utf-8").split('id="dashboard-data">', 1)[1].split("</script>", 1)[0].replace("<\\/", "</")
    )
    print(f"Dashboard: {out}")
    print(f"  {len(data['leads'])} khách hàng · {len(data['raw_posts'])} bài đã quét · {len(data['groups'])} nhóm")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

