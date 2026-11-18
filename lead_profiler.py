#!/usr/bin/env python3
"""AGENT-V1 — Deep Lead Profiler, Google Maps Checker & 1-10 Web Priority Scorer.

1. Deep-fetches up to 15-20 historical posts of a specific customer inside a Facebook
   group via their `group_member_url` (/groups/<gid>/user/<uid>/).
2. Consolidates clues across all collected posts (shop name, address, station, phone,
   posting hours, content style).
3. Looks up the shop on Google Maps (via Google Places API if configured, else
   Playwright Chromium search) and checks whether a Website / Landing Page is attached.
4. Audits any found website (HTTPS, status, mobile viewport) and scores the customer's
   Web / Landing Page need on a clear 1-10 scale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

# Force UTF-8 standard streams on Windows console/pipes
for _s in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from filters import (  # noqa: E402
    analyze_business_and_needs,
    analyze_lead_behavior,
    clean_fb_noise,
    classify_website_url,
    extract_fields,
    load_json,
    norm_vn,
    save_json,
    save_leads_csv,
    score_lead_web_priority,
)
from browser_worker import (  # noqa: E402
    _DOM_EXTRACT_CARDS_JS,
    _check_fb_session,
    _clean_post_text,
    _human_scroll_and_read,
    _playwright,
)


def _lead_ui_id(lead: dict) -> str:
    key = lead.get("lead_key") or (lead.get("personal_info") or {}).get("author_name") or ""
    return "L" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


def _extract_post_id_from_links(links: list[dict]) -> tuple[str, str]:
    for item in links or []:
        href = item.get("href") or ""
        for pat in (r"/posts/(\d+)", r"/permalink/(\d+)", r"multi_permalinks=(\d+)", r"story_fbid=(\d+)"):
            m = re.search(pat, href)
            if m:
                full = href if href.startswith("http") else f"https://www.facebook.com{href}"
                return m.group(1), full.split("?")[0]
    return "", ""


def fetch_member_group_posts(
    member_urls: list[str],
    state_path: Path,
    max_posts: int = 15,
    log_fn=print,
) -> list[dict]:
    """Visit the customer's group member activity page(s) to fetch historical posts."""
    urls = [u for u in dict.fromkeys(member_urls) if u and "/groups/" in u]
    if not urls:
        return []
    if not state_path.exists():
        raise RuntimeError(f"Chưa có phiên đăng nhập Facebook ({state_path.name}). Hãy bấm 'Đăng nhập FB' trước.")

    collected: dict[str, dict] = {}

    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()

        try:
            for url in urls[:2]:
                if len(collected) >= max_posts:
                    break
                m_gid = re.search(r"/groups/([^/]+)/", url)
                gid = m_gid.group(1) if m_gid else ""
                log_fn(f"🔎 Đang mở lịch sử bài đăng trong nhóm: {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=40000)
                except Exception:
                    pass
                page.wait_for_timeout(random.randint(3000, 4500))
                _check_fb_session(page, context)

                for scroll_idx in range(5):
                    if len(collected) >= max_posts:
                        break
                    for label in ("Xem thêm", "See more", "さらに表示"):
                        try:
                            btns = page.locator(f"div[role='button']:has-text('{label}')")
                            for idx in range(min(btns.count(), 4)):
                                try:
                                    b = btns.nth(idx)
                                    if b.is_visible():
                                        b.click(timeout=500)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    cards = page.evaluate(_DOM_EXTRACT_CARDS_JS)
                    before_n = len(collected)
                    for c in cards or []:
                        raw_txt = _clean_post_text(c.get("text") or "")
                        if len(raw_txt) < 25:
                            continue
                        pid, post_url = _extract_post_id_from_links(c.get("links") or [])
                        if not pid:
                            pid = "mem_" + hashlib.sha1(raw_txt[:150].encode("utf-8")).hexdigest()[:12]
                        if pid not in collected:
                            collected[pid] = {
                                "post_id": pid,
                                "post_url": post_url or url,
                                "group_id": gid,
                                "created_at": c.get("datetime") or "",
                                "content": clean_fb_noise(raw_txt),
                            }
                    if len(collected) >= max_posts or (scroll_idx >= 2 and len(collected) == before_n):
                        break
                    _human_scroll_and_read(page, total_pixels=1600, min_read_sec=2.0, max_read_sec=3.5)
        finally:
            browser.close()

    return list(collected.values())[:max_posts]


def audit_website_quick(url: str) -> dict:
    """Quick HTTP/HTML check to determine if an attached shop website is healthy & mobile-ready."""
    raw = (url or "").strip()
    if not raw:
        return {"status": "NO_WEB", "is_https": False, "mobile_ready": False, "http_code": 0}
    target = raw if "://" in raw else f"https://{raw}"
    is_https = target.lower().startswith("https://")
    req = urllib.request.Request(
        target,
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"},
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            code = getattr(resp, "status", 200) or 200
            final_url = resp.geturl() or target
            is_https = final_url.lower().startswith("https://")
            body = resp.read(32768).decode("utf-8", errors="ignore").lower()
            mobile_ready = "viewport" in body
            weak = (code >= 400) or (not is_https) or (not mobile_ready)
            return {
                "status": "HAS_WEB_WEAK" if weak else "HAS_WEB",
                "http_code": int(code),
                "is_https": is_https,
                "mobile_ready": mobile_ready,
                "final_url": final_url,
            }
    except Exception as exc:
        return {
            "status": "HAS_WEB_WEAK",
            "http_code": 0,
            "is_https": is_https,
            "mobile_ready": False,
            "error": str(exc)[:120],
        }


def _load_maps_api_key(root: Path) -> str:
    env_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if env_key:
        return env_key
    cfg_path = root / ".maps-config.json"
    if cfg_path.exists():
        try:
            cfg = load_json(cfg_path)
            if isinstance(cfg, dict) and cfg.get("api_key"):
                return str(cfg["api_key"]).strip()
        except Exception:
            pass
    return ""


def _search_maps_via_api(query: str, api_key: str) -> dict | None:
    """Query Google Places API (New Text Search v1) for exact place + websiteUri."""
    endpoint = "https://places.googleapis.com/v1/places:searchText"
    payload = json.dumps({"textQuery": query, "languageCode": "vi", "maxResultCount": 1}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.internationalPhoneNumber,places.websiteUri,places.rating,places.userRatingCount,places.googleMapsUri,places.primaryTypeDisplayName",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    places = data.get("places") or []
    if not places:
        return None
    pl = places[0]
    website = (pl.get("websiteUri") or "").strip()
    return {
        "place_name": (pl.get("displayName") or {}).get("text") or "",
        "address": pl.get("formattedAddress") or "",
        "phone": pl.get("internationalPhoneNumber") or "",
        "website": website,
        "website_status": classify_website_url(website),
        "rating": pl.get("rating"),
        "review_count": int(pl.get("userRatingCount") or 0),
        "category": (pl.get("primaryTypeDisplayName") or {}).get("text") or "",
        "maps_url": pl.get("googleMapsUri") or "",
    }


def _search_maps_via_playwright(queries: list[tuple[str, str]], log_fn=print) -> dict:
    """Search Google Maps using headless Chromium for zero-cost verification."""
    with _playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="vi-VN", viewport={"width": 1280, "height": 840})
        page = context.new_page()
        try:
            for query, confidence in queries:
                log_fn(f"📍 Đang tìm trên Google Maps: '{query}'...")
                url = f"https://www.google.com/maps/search/{quote(query)}?hl=vi"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                page.wait_for_timeout(random.randint(2500, 3800))

                body_low = ""
                try:
                    body_low = (page.locator("body").inner_text(timeout=2000) or "").lower()
                except Exception:
                    pass
                if any(m in body_low for m in ("unusual traffic", "not a robot", "captcha", "bất thường")):
                    return {
                        "status": "CHECK_ERROR",
                        "website_status": "UNCHECKED",
                        "error": "Google Maps yêu cầu xác minh (CAPTCHA) do tần suất truy vấn cao. Hãy thử lại sau ít phút.",
                        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }

                # Handle Google consent screen if shown
                try:
                    consent = page.locator("button:has-text('Accept all'), button:has-text('Tôi đồng ý'), button:has-text('Chấp nhận tất cả')").first
                    if consent.count() > 0 and consent.is_visible():
                        consent.click(timeout=2000)
                        page.wait_for_timeout(2500)
                except Exception:
                    pass

                # If search landed on a list of places, click the first result
                try:
                    first_card = page.locator("div[role='feed'] a.hfpxzc").first
                    if first_card.count() > 0 and first_card.is_visible():
                        first_card.click(timeout=4000)
                        page.wait_for_timeout(random.randint(2200, 3200))
                except Exception:
                    pass

                info = page.evaluate("""() => {
                    const h1 = document.querySelector("h1.DUwDvf, h1");
                    const placeName = h1 ? (h1.innerText || "").trim() : "";
                    if (!placeName || placeName.toLowerCase() === "kết quả" || placeName.toLowerCase() === "results") {
                        return null;
                    }
                    const addrBtn = document.querySelector("button[data-item-id='address'], [data-tooltip*='Sao chép địa chỉ'], [data-tooltip*='Copy address']");
                    const address = addrBtn ? (addrBtn.getAttribute("aria-label") || addrBtn.innerText || "").replace(/^Địa chỉ:\\s*/i, "").replace(/^Address:\\s*/i, "").trim() : "";
                    const phoneBtn = document.querySelector("button[data-item-id^='phone:tel:']");
                    const phone = phoneBtn ? (phoneBtn.getAttribute("aria-label") || phoneBtn.innerText || "").replace(/^Điện thoại:\\s*/i, "").replace(/^Phone:\\s*/i, "").trim() : "";
                    const webLink = document.querySelector("a[data-item-id='authority'], a[aria-label*='Trang web:'], a[aria-label*='Website:']");
                    const website = webLink ? (webLink.getAttribute("href") || "").trim() : "";
                    const ratingEl = document.querySelector("div.F7nice span[aria-hidden='true']");
                    const ratingText = ratingEl ? ratingEl.innerText.replace(",", ".").trim() : "";
                    const revEl = document.querySelector("div.F7nice span[aria-label*='bài đánh giá'], div.F7nice span[aria-label*='reviews']");
                    const revText = revEl ? (revEl.getAttribute("aria-label") || revEl.innerText || "") : "";
                    const catBtn = document.querySelector("button.DkEaL");
                    const category = catBtn ? (catBtn.innerText || "").trim() : "";
                    return {
                        place_name: placeName,
                        address,
                        phone,
                        website,
                        ratingText,
                        revText,
                        category,
                        maps_url: window.location.href,
                    };
                }""")

                if info and info.get("place_name"):
                    rating = None
                    try:
                        if info.get("ratingText"):
                            rating = float(info["ratingText"])
                    except Exception:
                        pass
                    review_count = 0
                    m_rev = re.search(r"([\d.,]+)", info.get("revText") or "")
                    if m_rev:
                        try:
                            review_count = int(re.sub(r"[.,]", "", m_rev.group(1)))
                        except Exception:
                            pass
                    website = info.get("website") or ""
                    return {
                        "status": "FOUND",
                        "place_name": info["place_name"],
                        "address": info.get("address") or "",
                        "phone": info.get("phone") or "",
                        "website": website,
                        "website_status": classify_website_url(website),
                        "rating": rating,
                        "review_count": review_count,
                        "category": info.get("category") or "",
                        "maps_url": info.get("maps_url") or url,
                        "query_used": query,
                        "match_confidence": confidence,
                        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
        finally:
            browser.close()

    return {
        "status": "NOT_FOUND",
        "website_status": "NOT_FOUND",
        "query_used": queries[0][0] if queries else "",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def build_maps_search_queries(lead: dict, behavior: dict) -> list[tuple[str, str]]:
    """Build ordered (query, confidence_label) pairs for Google Maps lookup."""
    queries: list[tuple[str, str]] = []
    p_info = lead.get("personal_info") or {}
    b_model = lead.get("step1_business_model") or {}

    shop_names = [s for s in (behavior.get("shop_names") or []) if s] or ([b_model["shop_name"]] if b_model.get("shop_name") else [])
    stations = [s for s in (behavior.get("stations") or []) if s] or (b_model.get("stations") or [])
    areas = [a.replace(" (theo nhóm)", "") for a in ((behavior.get("areas") or []) or (b_model.get("areas") or [])) if a]
    phones = [ph for ph in (behavior.get("phones") or []) if ph] or (p_info.get("all_phones") or ([p_info["phone"]] if p_info.get("phone") else []))
    loc = b_model.get("location") or (stations[0] if stations else (areas[0] if areas else "Japan"))

    for shop in shop_names[:2]:
        q = f"{shop} {loc}".strip()
        if q:
            queries.append((q, "Tên quán + Khu vực"))

    for ph in phones[:2]:
        digits = re.sub(r"[^\d+]", "", ph)
        if len(digits) >= 9:
            queries.append((ph, "Số điện thoại"))

    # If still no shop_name and no phone, try postal code / address clue if explicit Japanese address exists
    for addr in (behavior.get("address_clues") or [])[:1]:
        if re.search(r"(?:〒|\d+-\d+-\d+|[都道府県])", addr):
            queries.append((addr, "Địa chỉ từ bài đăng"))

    return queries


def check_google_maps_for_lead(lead: dict, behavior: dict, root: Path, log_fn=print) -> dict:
    """Look up shop on Google Maps and audit attached website if any."""
    queries = build_maps_search_queries(lead, behavior)
    if not queries:
        return {
            "status": "SKIPPED_NO_CLUES",
            "website_status": "UNCHECKED",
            "error": "Chưa tìm thấy Tên quán, SĐT hoặc Địa chỉ cụ thể từ bài đăng để tra Google Maps.",
            "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    api_key = _load_maps_api_key(root)
    result: dict | None = None
    if api_key:
        for query, conf in queries:
            try:
                log_fn(f"📍 [Places API] Đang tra Google Maps: '{query}'...")
                hit = _search_maps_via_api(query, api_key)
                if hit:
                    hit.update({
                        "status": "FOUND",
                        "query_used": query,
                        "match_confidence": conf,
                        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    result = hit
                    break
            except Exception as exc:
                log_fn(f"⚠️ Places API lỗi ({exc}), chuyển sang kiểm tra trực tiếp...")
                break
        if result is None and not api_key:
            result = None

    if result is None:
        result = _search_maps_via_playwright(queries, log_fn=log_fn)

    if result.get("status") == "FOUND" and result.get("website_status") == "HAS_WEB" and result.get("website"):
        log_fn(f"🌐 Đang kiểm tra chất lượng website: {result['website']}...")
        audit = audit_website_quick(result["website"])
        result["website_audit"] = audit
        if audit.get("status") == "HAS_WEB_WEAK":
            result["website_status"] = "HAS_WEB_WEAK"

    return result


def _enrich_lead_from_posts(lead: dict, full_posts_map: dict[str, dict]) -> dict:
    """Re-run extraction & business analysis over all posts of a lead to uncover hidden clues."""
    p_info = lead.setdefault("personal_info", {})
    b_model = lead.setdefault("step1_business_model", {})
    d_needs = lead.setdefault("step2_deep_needs", {})
    posts = lead.get("source_posts") or []

    all_phones = list(p_info.get("all_phones") or [])
    for sp in posts:
        pid = str(sp.get("post_id") or "")
        text = (full_posts_map.get(pid) or {}).get("content") or sp.get("content") or sp.get("content_snippet") or ""
        if not text:
            continue
        analysis = analyze_business_and_needs(text, author_name=p_info.get("author_name") or "")
        contact_info = analysis.get("contact") or {}
        for ph in contact_info.get("all_phones") or ([contact_info["phone"]] if contact_info.get("phone") else []):
            if ph and ph not in all_phones:
                all_phones.append(ph)
                if not p_info.get("phone"):
                    p_info["phone"] = ph
        if contact_info.get("line_id") and not p_info.get("line_id"):
            p_info["line_id"] = contact_info["line_id"]
        if contact_info.get("zalo_id") and not p_info.get("zalo_id"):
            p_info["zalo_id"] = contact_info["zalo_id"]
        if analysis.get("shop_name") and not b_model.get("shop_name"):
            b_model["shop_name"] = analysis["shop_name"]
        if analysis.get("location") and not b_model.get("location"):
            b_model["location"] = analysis["location"]
        for st in analysis.get("stations") or []:
            if st not in b_model.setdefault("stations", []):
                b_model["stations"].append(st)
        for ar in analysis.get("areas") or []:
            if ar not in b_model.setdefault("areas", []):
                b_model["areas"].append(ar)
        for r in analysis.get("roles_needed") or []:
            if r not in d_needs.setdefault("roles_needed", []):
                d_needs["roles_needed"].append(r)

    p_info["all_phones"] = all_phones
    return lead


def profile_single_lead(
    lead_key_or_id: str,
    root: Path | None = None,
    deep_fb: bool = True,
    check_maps: bool = True,
    log_fn=print,
) -> dict:
    """Run deep FB profiling + Google Maps check + 1-10 Web priority scoring for one lead."""
    root = Path(root or ROOT)
    leads_path = root / "leads.json"
    if not leads_path.exists():
        raise FileNotFoundError("Chưa có file leads.json để phân tích.")

    leads_doc = load_json(leads_path)
    leads_list = leads_doc.get("leads", []) if isinstance(leads_doc, dict) else leads_doc
    target_idx = -1
    for i, l in enumerate(leads_list):
        if l.get("lead_key") == lead_key_or_id or _lead_ui_id(l) == lead_key_or_id:
            target_idx = i
            break
    if target_idx == -1:
        raise ValueError(f"Không tìm thấy khách hàng '{lead_key_or_id}' trong leads.json")

    lead = leads_list[target_idx]
    p_info = lead.setdefault("personal_info", {})
    member_urls = [p_info.get("group_member_url")] if p_info.get("group_member_url") else []
    for sp in lead.get("source_posts") or []:
        gid = sp.get("group_id") or ""
        aurl = p_info.get("author_url") or ""
        m_uid = re.search(r"profile\.php\?id=(\d+)", aurl)
        if gid and m_uid:
            cand = f"https://www.facebook.com/groups/{gid}/user/{m_uid.group(1)}/"
            if cand not in member_urls:
                member_urls.append(cand)

    # Step 1: Deep FB fetch from group_member_url
    new_posts_added = 0
    if deep_fb and member_urls:
        state_path = root / "fb-state.json"
        fetched = fetch_member_group_posts(member_urls, state_path, max_posts=15, log_fn=log_fn)
        existing_ids = {str(sp.get("post_id") or "") for sp in lead.get("source_posts") or []}
        for fp in fetched:
            pid = str(fp.get("post_id") or "")
            if pid and pid not in existing_ids:
                lead.setdefault("source_posts", []).append({
                    "post_id": pid,
                    "post_url": fp.get("post_url") or "",
                    "group_id": fp.get("group_id") or "",
                    "group_name": "",
                    "created_at": fp.get("created_at") or "",
                    "content_snippet": (fp.get("content") or "")[:300],
                    "content": fp.get("content") or "",
                })
                existing_ids.add(pid)
                new_posts_added += 1
        log_fn(f"📚 Đã gom thêm {new_posts_added} bài đăng lịch sử của khách hàng này (tổng {len(lead.get('source_posts') or [])} bài).")
    elif deep_fb:
        log_fn("ℹ️ Khách hàng này chưa có link thành viên trong nhóm (group_member_url) — dùng các bài sẵn có để phân tích.")

    # Enrich from result.json full content if available
    res_path = root / "result.json"
    full_map: dict[str, dict] = {}
    if res_path.exists():
        try:
            res_doc = load_json(res_path)
            for p in (res_doc or {}).get("posts") or []:
                if p.get("post_id"):
                    full_map[str(p["post_id"])] = p
        except Exception:
            pass

    _enrich_lead_from_posts(lead, full_map)
    behavior = analyze_lead_behavior(lead.get("source_posts") or [], lead)
    lead["behavior_analysis"] = behavior
    lead["deep_profiled_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    # Step 2: Google Maps check
    maps_info = lead.get("maps_info") or {}
    if check_maps:
        maps_info = check_google_maps_for_lead(lead, behavior, root, log_fn=log_fn)
        lead["maps_info"] = maps_info
        log_fn(
            f"🗺️ Kết quả Google Maps: {maps_info.get('status')} | "
            f"Web: {maps_info.get('website_status')} {maps_info.get('website') or ''}"
        )

    # Step 3: Score 1-10
    web_priority = score_lead_web_priority(lead, behavior, maps_info)
    lead["web_priority"] = web_priority
    log_fn(f"🏆 Điểm Web/Landing Page: {web_priority['score']}/10 ({web_priority['tier_label']})")

    leads_list[target_idx] = lead
    save_json(leads_path, {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_leads": len(leads_list),
        "leads": leads_list,
    })
    try:
        save_leads_csv(root / "leads.csv", leads_list)
    except Exception:
        pass

    import dashboard
    dashboard.generate(root, save_leads=False)
    return {
        "ok": True,
        "lead_key": lead.get("lead_key"),
        "lead_id": _lead_ui_id(lead),
        "new_posts_added": new_posts_added,
        "behavior": behavior,
        "maps_info": maps_info,
        "web_priority": web_priority,
    }


def recompute_all_leads_scores(root: Path | None = None) -> int:
    """Fast offline enrichment + 1-10 scoring for every lead currently in leads.json."""
    root = Path(root or ROOT)
    leads_path = root / "leads.json"
    if not leads_path.exists():
        return 0
    doc = load_json(leads_path)
    leads_list = doc.get("leads", []) if isinstance(doc, dict) else doc
    res_path = root / "result.json"
    full_map: dict[str, dict] = {}
    if res_path.exists():
        try:
            res_doc = load_json(res_path)
            for p in (res_doc or {}).get("posts") or []:
                if p.get("post_id"):
                    full_map[str(p["post_id"])] = p
        except Exception:
            pass

    for lead in leads_list:
        _enrich_lead_from_posts(lead, full_map)
        beh = analyze_lead_behavior(lead.get("source_posts") or [], lead)
        lead["behavior_analysis"] = beh
        lead["web_priority"] = score_lead_web_priority(lead, beh, lead.get("maps_info"))

    save_json(leads_path, {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_leads": len(leads_list),
        "leads": leads_list,
    })
    save_leads_csv(root / "leads.csv", leads_list)
    import dashboard
    dashboard.generate(root, save_leads=False)
    return len(leads_list)


def main() -> int:
    parser = argparse.ArgumentParser(description="AGENT-V1 Deep Lead Profiler & Google Maps Web Scorer")
    parser.add_argument("--lead-key", help="lead_key or UI id (L...) to profile")
    parser.add_argument("--no-fb", action="store_true", help="Skip Facebook member post fetching")
    parser.add_argument("--no-maps", action="store_true", help="Skip Google Maps check")
    parser.add_argument("--recompute-all", action="store_true", help="Recompute behavior + 1-10 scores offline for all leads")
    args = parser.parse_args()

    if args.recompute_all:
        cnt = recompute_all_leads_scores(ROOT)
        print(f"✅ Đã tính lại điểm hành vi & thang điểm 1–10 cho {cnt} khách hàng!")
        return 0
    if args.lead_key:
        res = profile_single_lead(
            args.lead_key,
            root=ROOT,
            deep_fb=not args.no_fb,
            check_maps=not args.no_maps,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
