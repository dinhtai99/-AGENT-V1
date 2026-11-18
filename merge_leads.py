#!/usr/bin/env python3
"""AGENT-V1 — Merge Leads & Scan Results from Multiple Machines (Mac & PC).

Combines leads from Mac and PC without duplicate entries:
- Matches leads by Facebook Profile URL or normalized Author Name.
- Merges all source posts, contacts (phone, Line, Zalo), and business profiles.
- Preserves manual CRM statuses (Hot, Contacted, Won) and customer notes.
- Automatically refreshes leads.json, leads.csv (Excel), and dashboard.html.

Usage:
    python3 merge_leads.py leads-pc.json
    python3 merge_leads.py path/to/pc/leads.json path/to/pc/result.json
    python3 merge_leads.py             # defaults to looking for 'leads-pc.json'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
from datetime import datetime
from pathlib import Path

# Force UTF-8 standard streams on Windows console/pipes
for _s in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
    if _s and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from filters import norm_vn, prune_disqualified_leads, save_json, save_leads_csv  # noqa: E402
import dashboard  # noqa: E402


def _clean_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"profile\.php\?id=(\d+)", url)
    if m:
        return f"https://www.facebook.com/profile.php?id={m.group(1)}"
    m_user = re.search(r"/user/(\d+)", url)
    if m_user:
        return f"https://www.facebook.com/profile.php?id={m_user.group(1)}"
    return url.split("?")[0].rstrip("/")


def _make_key(lead: dict) -> str:
    p_info = lead.get("personal_info") or {}
    url = _clean_url(p_info.get("author_url") or "")
    if url:
        return f"url:{url}"
    name = norm_vn(p_info.get("author_name") or "")
    if name and name not in {"", "chua ro ten fb", "thanh vien an danh"}:
        return f"name:{name}"
    # Fallback to first source post or lead_id
    posts = lead.get("source_posts") or []
    if posts and posts[0].get("post_id"):
        return f"post:{posts[0]['post_id']}"
    return f"id:{lead.get('lead_id') or id(lead)}"


def _extract_leads_list(doc: dict | list) -> list[dict]:
    if isinstance(doc, dict):
        if "leads" in doc and isinstance(doc["leads"], list):
            return doc["leads"]
        if "posts" in doc and isinstance(doc["posts"], list):
            # It's a result.json file, build leads list from posts
            from filters import build_leads_list
            return build_leads_list(doc["posts"])
        return []
    elif isinstance(doc, list):
        return doc
    return []


def merge_two_leads(base: dict, incoming: dict) -> bool:
    """Merge incoming lead into base lead in-place. Returns True if updated."""
    updated = False

    # 1. Personal info
    b_p = base.setdefault("personal_info", {})
    i_p = incoming.get("personal_info") or {}
    if not b_p.get("author_name") and i_p.get("author_name"):
        b_p["author_name"] = i_p["author_name"]
        updated = True
    if not b_p.get("author_url") and i_p.get("author_url"):
        b_p["author_url"] = i_p["author_url"]
        updated = True
    if not b_p.get("phone") and i_p.get("phone"):
        b_p["phone"] = i_p["phone"]
        updated = True
    if not b_p.get("line_id") and i_p.get("line_id"):
        b_p["line_id"] = i_p["line_id"]
        updated = True
    if not b_p.get("zalo_id") and i_p.get("zalo_id"):
        b_p["zalo_id"] = i_p["zalo_id"]
        updated = True

    b_phones = set(b_p.setdefault("all_phones", []))
    for ph in i_p.get("all_phones") or []:
        if ph and ph not in b_phones:
            b_p["all_phones"].append(ph)
            b_phones.add(ph)
            updated = True

    # 2. Step 1: Business model
    b_m = base.setdefault("step1_business_model", {})
    i_m = incoming.get("step1_business_model") or {}
    for k in ("business_type", "shop_name", "location", "estimated_scale"):
        if not b_m.get(k) and i_m.get(k):
            b_m[k] = i_m[k]
            updated = True

    for list_k in ("stations", "areas", "all_business_types"):
        b_list = set(b_m.setdefault(list_k, []))
        for item in i_m.get(list_k) or []:
            if item and item not in b_list:
                b_m[list_k].append(item)
                b_list.add(item)
                updated = True

    # 3. Step 2: Deep needs
    b_n = base.setdefault("step2_deep_needs", {})
    i_n = incoming.get("step2_deep_needs") or {}
    if not b_n.get("wage_offered") and i_n.get("wage_offered"):
        b_n["wage_offered"] = i_n["wage_offered"]
        updated = True

    for list_k in ("roles_needed", "shifts_needed", "pain_points_and_needs", "benefits_offered", "target_customer_segments"):
        b_list = set(b_n.setdefault(list_k, []))
        for item in i_n.get(list_k) or []:
            if item and item not in b_list:
                b_n[list_k].append(item)
                b_list.add(item)
                updated = True

    # 4. Source posts
    b_posts = base.setdefault("source_posts", [])
    seen_posts = {sp.get("post_id") or sp.get("post_url") for sp in b_posts}
    for sp in incoming.get("source_posts") or []:
        sp_id = sp.get("post_id") or sp.get("post_url")
        if sp_id and sp_id not in seen_posts:
            b_posts.append(sp)
            seen_posts.add(sp_id)
            updated = True

    # 5. CRM status & notes
    b_st = base.get("crm_status", "new")
    i_st = incoming.get("crm_status", "new")
    crm_priority = {"won": 5, "hot": 4, "contacted": 3, "skip": 2, "new": 1}
    if crm_priority.get(i_st, 0) > crm_priority.get(b_st, 0):
        base["crm_status"] = i_st
        updated = True

    b_notes = (base.get("crm_notes") or "").strip()
    i_notes = (incoming.get("crm_notes") or "").strip()
    if i_notes and i_notes not in b_notes:
        base["crm_notes"] = f"{b_notes} | {i_notes}".strip(" |")
        updated = True

    if updated:
        base["last_updated"] = datetime.now().isoformat()

    return updated


def merge_files(incoming_paths: list[Path], target_path: Path | None = None, rebuild_dash: bool = True) -> tuple[int, int, int]:
    """Merge incoming leads files into master leads.json.

    Returns (total_before, newly_added, updated_existing).
    """
    if target_path is None:
        target_path = ROOT / "leads.json"

    disq_keys: set[str] = set()
    master_leads: list[dict] = []
    if target_path.exists():
        try:
            doc = json.loads(target_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                disq_keys.update(str(k) for k in (doc.get("disqualified_keys") or []) if str(k))
            master_leads = _extract_leads_list(doc)
        except Exception as exc:
            print(f"⚠️ Cảnh báo đọc file {target_path}: {exc}")

    leads_map: dict[str, dict] = {}
    for l in master_leads:
        leads_map[_make_key(l)] = l

    total_before = len(leads_map)
    newly_added = 0
    updated_existing = 0

    for inc_path in incoming_paths:
        if not inc_path.exists():
            print(f"❌ Không tìm thấy file: {inc_path}")
            continue
        try:
            doc = json.loads(inc_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                disq_keys.update(str(k) for k in (doc.get("disqualified_keys") or []) if str(k))
            incoming_leads = _extract_leads_list(doc)
            print(f"📂 Đang gộp dữ liệu từ '{inc_path.name}' ({len(incoming_leads)} khách hàng)...")
            for inc in incoming_leads:
                key = _make_key(inc)
                if key in leads_map:
                    if merge_two_leads(leads_map[key], inc):
                        updated_existing += 1
                else:
                    leads_map[key] = inc
                    newly_added += 1
        except Exception as exc:
            print(f"❌ Lỗi xử lý file {inc_path}: {exc}")

    combined, disq_list, _ = prune_disqualified_leads(list(leads_map.values()), disq_keys)
    save_json(target_path, {
        "updated_at": datetime.now().isoformat(),
        "total_leads": len(combined),
        "disqualified_keys": disq_list,
        "leads": combined,
    })

    # Export to Excel CSV
    csv_path = target_path.with_suffix(".csv")
    try:
        save_leads_csv(csv_path, combined)
    except Exception as exc:
        print(f"⚠️ Cảnh báo lưu leads.csv: {exc}")

    # Rebuild Dashboard
    if rebuild_dash:
        try:
            print("📊 Đang làm mới Dashboard HTML với dữ liệu đã gộp...")
            dashboard.generate(ROOT, save_leads=False)
        except Exception as exc:
            print(f"⚠️ Cảnh báo tạo dashboard.html: {exc}")

    return total_before, newly_added, updated_existing


def git_merge(ancestor_path: Path, current_path: Path, other_path: Path) -> int:
    """Git 3-way custom merge driver for leads.json.

    Merges other_path (%B) into current_path (%A).
    Git requires output written to current_path (%A) and returncode 0.
    """
    try:
        print(f"🤝 [Git Merge Driver] Đang tự động gộp dữ liệu leads.json giữa 2 máy...")
        merge_files([other_path], target_path=current_path, rebuild_dash=False)
        print(f"✅ [Git Merge Driver] Hợp nhất leads.json thành công, 0 xung đột!")
        return 0
    except Exception as exc:
        print(f"❌ [Git Merge Driver] Lỗi: {exc}", file=sys.stderr)
        return 1


def setup_git_driver() -> int:
    """Configure Git repository to use this merge driver automatically."""
    import subprocess
    cmd = "python merge_leads.py" if sys.platform == "win32" else "python3 merge_leads.py"
    try:
        subprocess.check_call(["git", "config", "merge.json-leads.name", "JSON Leads 3-Way Merger"], cwd=str(ROOT))
        subprocess.check_call(["git", "config", "merge.json-leads.driver", f"{cmd} --git-merge %O %A %B"], cwd=str(ROOT))
        print("✅ Đã cài đặt Git Merge Driver thành công cho repository!")
        return 0
    except Exception as exc:
        print(f"❌ Không thể cấu hình Git: {exc}")
        return 1


def main():
    parser = argparse.ArgumentParser(description="Hợp nhất dữ liệu khách hàng từ nhiều máy (Mac & PC).")
    parser.add_argument("--git-merge", nargs=3, metavar=("ANCESTOR", "CURRENT", "OTHER"),
                        help="Chế độ Git Merge Driver tự động (dùng cho lệnh git pull / git merge).")
    parser.add_argument("--setup-git", action="store_true", help="Cài đặt Git Merge Driver cho repository.")
    parser.add_argument("files", nargs="*", help="Đường dẫn đến các file leads.json hoặc result.json từ máy khác.")
    args = parser.parse_args()

    if args.setup_git:
        return setup_git_driver()

    if args.git_merge:
        ancestor, current, other = [Path(p) for p in args.git_merge]
        return git_merge(ancestor, current, other)

    files = [Path(f) for f in args.files]
    if not files:
        candidate = ROOT / "leads-pc.json"
        if candidate.exists():
            files = [candidate]
        else:
            print("=================================================================")
            print("  🤝 AGENT-V1 — CÔNG CỤ HỢP NHẤT DỮ LIỆU MAC & PC")
            print("=================================================================")
            print("👉 Cách 1 (Git tự động): Nhấp đúp file 'sync-git.command' (hoặc 'sync-git.bat' trên PC)")
            print("👉 Cách 2 (Thủ công): Chép leads.json từ PC về Mac thành 'leads-pc.json'")
            print("   rồi chạy: python3 merge_leads.py leads-pc.json")
            print("=================================================================")
            return 1

    total_before, added, updated = merge_files(files)
    print("\n=================================================================")
    print("  ✅ HỢP NHẤT DỮ LIỆU HOÀN TẤT THÀNH CÔNG!")
    print(f"  • Số khách ban đầu trên Mac: {total_before}")
    print(f"  • Khách hàng MỚI bổ sung từ PC: +{added}")
    print(f"  • Khách hàng được cập nhật thêm tin: {updated}")
    print(f"  • TỔNG CỘNG KHÁCH HÀNG SAU GỘP: {total_before + added}")
    print("=================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
