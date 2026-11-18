#!/usr/bin/env python3
"""Two-way Git sync & GitHub Pages deployer (Mac <-> Windows shared data).

1. Pulls latest `origin/gh-pages` before deploying or scanning.
2. Merges `leads.json` (including CRM status/notes), `result.json` (posts & groups),
   and `seen.json` between Mac and Windows so neither machine ever overwrites
   the other's leads or re-scans already-seen posts.
3. Also extracts leads/CRM/posts from `origin/gh-pages:index.html` as a fallback
   if the other machine pushed using an older version of `deploy_github.py`.
4. Regenerates `dashboard.html` / `dist/index.html` and pushes the unified
   bundle (`index.html`, `.nojekyll`, `leads.json`, `result.json`, `seen.json`)
   to `refs/heads/gh-pages` using zero-disruption git plumbing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

from filters import load_json, prune_disqualified_leads, save_json, save_leads_csv  # noqa: E402


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "AGENT-V1 Bot"),
        "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "agent-v1@users.noreply.github.com"),
        "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "AGENT-V1 Bot"),
        "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "agent-v1@users.noreply.github.com"),
    }


def _git_show_json(root: Path, ref_path: str) -> dict | list | None:
    """Read a JSON blob from git (e.g. 'origin/gh-pages:leads.json') without checkout."""
    try:
        raw = subprocess.check_output(
            ["git", "show", ref_path],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            env=_git_env(),
        ).decode("utf-8", errors="replace")
        return json.loads(raw)
    except Exception:
        return None


def _git_show_text(root: Path, ref_path: str) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "show", ref_path],
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            env=_git_env(),
        ).decode("utf-8", errors="replace")
    except Exception:
        return None


def _extract_dashboard_data_from_html(html: str) -> dict | None:
    """Fallback: extract embedded dashboard JSON from index.html."""
    marker = 'id="dashboard-data">'
    if marker not in html:
        return None
    try:
        payload = html.split(marker, 1)[1].split("</script>", 1)[0].replace("<\\/", "</").replace("<\\!--", "<!--")
        return json.loads(payload)
    except Exception:
        return None


def _merge_crm(local_crm: dict | None, remote_crm: dict | None) -> dict:
    """Merge CRM records from two machines; newer updated_at or non-empty fields win."""
    c1 = dict(local_crm or {})
    c2 = dict(remote_crm or {})
    if not c1:
        return c2
    if not c2:
        return c1

    t1 = str(c1.get("updated_at") or "")
    t2 = str(c2.get("updated_at") or "")
    primary, secondary = (c2, c1) if t2 > t1 else (c1, c2)

    merged = dict(secondary)
    for k, v in primary.items():
        if v not in (None, ""):
            merged[k] = v

    # Never let a default 'new' overwrite an active status ('contacted', 'hot', 'won', 'skip')
    # unless the primary timestamp is strictly newer
    s1 = c1.get("status") or "new"
    s2 = c2.get("status") or "new"
    if s1 != "new" and s2 == "new" and not (t2 > t1 and t1):
        merged["status"] = s1
    elif s2 != "new" and s1 == "new" and not (t1 > t2 and t2):
        merged["status"] = s2

    return merged


def merge_two_lead_lists(local_leads: list[dict], remote_leads: list[dict]) -> list[dict]:
    """Merge two lists of lead dicts (from leads.json) without losing any lead, post, or CRM."""
    by_key: dict[str, dict] = {}

    for lead in local_leads:
        if not isinstance(lead, dict):
            continue
        k = lead.get("lead_key") or (lead.get("personal_info") or {}).get("author_url")
        if k:
            by_key[str(k)] = json.loads(json.dumps(lead))

    for r_lead in remote_leads:
        if not isinstance(r_lead, dict):
            continue
        k = r_lead.get("lead_key") or (r_lead.get("personal_info") or {}).get("author_url")
        if not k:
            continue
        k = str(k)
        if k not in by_key:
            by_key[k] = json.loads(json.dumps(r_lead))
            continue

        l_lead = by_key[k]
        # 1. Merge source_posts
        l_posts = l_lead.setdefault("source_posts", [])
        seen_pids = {str(p.get("post_id") or p.get("post_url") or "") for p in l_posts}
        for sp in r_lead.get("source_posts") or []:
            pid = str(sp.get("post_id") or sp.get("post_url") or "")
            if pid and pid not in seen_pids:
                l_posts.append(sp)
                seen_pids.add(pid)
        l_lead["posts_count"] = len(l_posts)

        # 2. Merge personal_info
        lp = l_lead.setdefault("personal_info", {})
        rp = r_lead.get("personal_info") or {}
        for field in ("author_name", "author_url", "group_member_url", "phone", "line_id", "zalo_id"):
            if not lp.get(field) and rp.get(field):
                lp[field] = rp[field]
        all_phones = list(dict.fromkeys((lp.get("all_phones") or []) + (rp.get("all_phones") or [])))
        if all_phones:
            lp["all_phones"] = all_phones
        methods = list(dict.fromkeys((lp.get("contact_methods") or []) + (rp.get("contact_methods") or [])))
        if methods:
            lp["contact_methods"] = methods

        # 3. Merge CRM
        merged_crm = _merge_crm(l_lead.get("crm"), r_lead.get("crm"))
        if merged_crm:
            l_lead["crm"] = merged_crm

    return list(by_key.values())


def sync_from_github(root: Path | None = None, fetch: bool = True) -> tuple[bool, str]:
    """Fetch `origin/gh-pages` and merge remote leads, posts, and seen IDs into local files."""
    root = Path(root or ROOT)
    if not (root / ".git").exists():
        return False, "Thư mục chưa có .git"

    if fetch:
        try:
            subprocess.run(
                ["git", "fetch", "origin", "gh-pages"],
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=25,
                env=_git_env(),
            )
        except Exception as exc:
            return False, f"Không thể fetch từ GitHub: {exc}"

    # 1. Read remote leads.json, result.json, seen.json if available
    remote_leads_doc = _git_show_json(root, "origin/gh-pages:leads.json")
    remote_result_doc = _git_show_json(root, "origin/gh-pages:result.json")
    remote_seen_doc = _git_show_json(root, "origin/gh-pages:seen.json")

    remote_leads: list[dict] = []
    if isinstance(remote_leads_doc, dict):
        remote_leads = remote_leads_doc.get("leads") or []
    elif isinstance(remote_leads_doc, list):
        remote_leads = remote_leads_doc

    # Fallback: also inspect origin/gh-pages:index.html in case the other machine
    # pushed with an older deploy_github.py that only uploaded index.html
    remote_html = _git_show_text(root, "origin/gh-pages:index.html")
    html_data = _extract_dashboard_data_from_html(remote_html) if remote_html else None

    # 2. Merge seen.json
    if isinstance(remote_seen_doc, dict):
        seen_path = root / "seen.json"
        local_seen = load_json(seen_path) if seen_path.exists() else {}
        if not isinstance(local_seen, dict):
            local_seen = {}
        for gid, r_ids in remote_seen_doc.items():
            if isinstance(r_ids, list):
                l_ids = local_seen.get(str(gid)) or []
                local_seen[str(gid)] = sorted(set(map(str, l_ids)) | set(map(str, r_ids)))[-500:]
        save_json(seen_path, local_seen)

    # 3. Merge result.json (posts & groups)
    res_path = root / "result.json"
    local_res = load_json(res_path) if res_path.exists() else {}
    if not isinstance(local_res, dict):
        local_res = {"task_id": "TASK-BAITO-JP-001", "status": "success", "errors": [], "posts": [], "leads": [], "groups": []}

    posts_by_id = {str(p.get("post_id")): p for p in (local_res.get("posts") or []) if isinstance(p, dict) and p.get("post_id")}
    groups_by_id = {str(g.get("id")): g for g in (local_res.get("groups") or []) if isinstance(g, dict) and g.get("id")}

    if isinstance(remote_result_doc, dict):
        for p in remote_result_doc.get("posts") or []:
            pid = str(p.get("post_id") or "")
            if pid and pid not in posts_by_id:
                posts_by_id[pid] = p
        for g in remote_result_doc.get("groups") or []:
            gid = str(g.get("id") or "")
            if gid and (gid not in groups_by_id or (g.get("raw_posts_read", 0) > groups_by_id[gid].get("raw_posts_read", 0))):
                groups_by_id[gid] = g

    if isinstance(html_data, dict):
        for l_item in html_data.get("leads") or []:
            for p_item in l_item.get("posts") or []:
                pid = str(p_item.get("post_id") or "")
                if pid and pid not in posts_by_id:
                    posts_by_id[pid] = {
                        "post_id": pid,
                        "post_url": p_item.get("url") or "",
                        "group_id": (l_item.get("group_ids") or [""])[0],
                        "author_name": l_item.get("author_name") or "",
                        "author_url": l_item.get("fb_url") or "",
                        "group_member_url": l_item.get("member_url") or "",
                        "created_at": p_item.get("created_at") or "",
                        "content": p_item.get("content") or "",
                    }

    if posts_by_id or groups_by_id:
        local_res["posts"] = list(posts_by_id.values())
        local_res["groups"] = list(groups_by_id.values())
        save_json(res_path, local_res)

    # 4. Merge leads.json (including CRM from html_data if remote_leads wasn't present)
    leads_path = root / "leads.json"
    local_leads_doc = load_json(leads_path) if leads_path.exists() else {}
    local_leads = (
        local_leads_doc.get("leads", [])
        if isinstance(local_leads_doc, dict)
        else local_leads_doc if isinstance(local_leads_doc, list) else []
    )
    before_count = len(local_leads)

    if remote_leads:
        merged_leads = merge_two_lead_lists(local_leads, remote_leads)
    else:
        merged_leads = list(local_leads)

    # Also apply any newer CRM edits from html_data
    if isinstance(html_data, dict):
        html_crm_map = {
            str(item.get("lead_key")): item.get("crm")
            for item in (html_data.get("leads") or [])
            if isinstance(item, dict) and item.get("lead_key") and item.get("crm")
        }
        for lead in merged_leads:
            lk = str(lead.get("lead_key") or "")
            if lk in html_crm_map:
                lead["crm"] = _merge_crm(lead.get("crm"), html_crm_map[lk])

    local_disq = set(local_leads_doc.get("disqualified_keys") or []) if isinstance(local_leads_doc, dict) else set()
    remote_disq = set(remote_leads_doc.get("disqualified_keys") or []) if isinstance(remote_leads_doc, dict) else set()
    merged_leads, disq_list, _ = prune_disqualified_leads(merged_leads, local_disq | remote_disq)

    save_json(
        leads_path,
        {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_leads": len(merged_leads),
            "disqualified_keys": disq_list,
            "leads": merged_leads,
        },
    )
    try:
        save_leads_csv(root / "leads.csv", merged_leads)
    except Exception:
        pass

    added = max(0, len(merged_leads) - before_count)
    return True, f"Đồng bộ thành công: tổng {len(merged_leads)} khách hàng (+{added} khách mới từ máy kia)."


def _hash_file_or_bytes(root: Path, path: Path | None = None, data: bytes = b"") -> str:
    if path is not None and path.exists():
        return subprocess.check_output(
            ["git", "hash-object", "-w", str(path)],
            cwd=str(root),
            stderr=subprocess.PIPE,
            env=_git_env(),
        ).decode("utf-8", errors="replace").strip()
    return subprocess.check_output(
        ["git", "hash-object", "-w", "--stdin"],
        input=data,
        cwd=str(root),
        stderr=subprocess.PIPE,
        env=_git_env(),
    ).decode("utf-8", errors="replace").strip()


def deploy(html_path: Path | None = None) -> tuple[bool, str]:
    if not (ROOT / ".git").exists():
        return False, "Thư mục hiện tại chưa khởi tạo Git (.git không tồn tại)."

    # 1. Pull & merge latest leads/CRM/posts from the other machine (Mac <-> Windows)
    sync_ok, sync_msg = sync_from_github(ROOT, fetch=True)

    # 2. Rebuild dashboard.html and dist/index.html with unified data
    try:
        import importlib
        import dashboard
        importlib.reload(dashboard)
        dashboard.generate(ROOT, save_leads=True)
    except Exception:
        pass

    if html_path is None:
        html_path = ROOT / "dist" / "index.html"
    if not html_path.exists():
        html_path = ROOT / "dashboard.html"

    if not html_path.exists():
        return False, "Không tìm thấy file dashboard.html hoặc dist/index.html"

    try:
        git_env = _git_env()
        blob_html = _hash_file_or_bytes(ROOT, path=html_path)
        blob_nojekyll = _hash_file_or_bytes(ROOT, data=b"")

        entries = [
            f"100644 blob {blob_html}\tindex.html",
            f"100644 blob {blob_nojekyll}\t.nojekyll",
        ]

        # Include leads.json, result.json, seen.json in gh-pages so Mac & Windows
        # can two-way merge structured JSON directly on every sync!
        for fname in ("leads.json", "result.json", "seen.json"):
            fpath = ROOT / fname
            if fpath.exists():
                b_hash = _hash_file_or_bytes(ROOT, path=fpath)
                entries.append(f"100644 blob {b_hash}\t{fname}")

        tree_input = ("\n".join(entries) + "\n").encode("utf-8")

        proc = subprocess.Popen(
            ["git", "mktree"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(ROOT),
            env=git_env,
        )
        tree_out, tree_err = proc.communicate(tree_input)
        if proc.returncode != 0:
            return False, f"Lỗi tạo git tree: {tree_err.decode('utf-8', errors='replace')}"
        tree_hash = tree_out.decode("utf-8", errors="replace").strip()

        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        commit_hash = subprocess.check_output(
            ["git", "commit-tree", tree_hash, "-m", f"Deploy & Sync GitHub Pages: {now_str}"],
            cwd=str(ROOT),
            stderr=subprocess.PIPE,
            env=git_env,
        ).decode("utf-8", errors="replace").strip()

        subprocess.check_output(
            ["git", "push", "origin", f"{commit_hash}:refs/heads/gh-pages", "--force"],
            cwd=str(ROOT),
            stderr=subprocess.STDOUT,
            env=git_env,
        ).decode("utf-8", errors="replace")

        pages_url = "https://dinhtai99.github.io/-AGENT-V1/"
        return True, f"{sync_msg} Đã đẩy lên GitHub Pages: {pages_url}"

    except subprocess.CalledProcessError as exc:
        err_msg = exc.output.decode("utf-8", errors="replace") if exc.output else str(exc)
        return False, f"Lỗi git push: {err_msg.strip()}"
    except Exception as exc:
        return False, f"Lỗi deploy GitHub Pages: {exc}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Đồng bộ dữ liệu Mac <-> Windows và đẩy lên GitHub Pages")
    parser.add_argument("--sync-only", action="store_true", help="Chỉ kéo và gộp dữ liệu từ GitHub về máy, không push")
    args = parser.parse_args(argv)

    if args.sync_only:
        print("🔄 Đang kéo và gộp dữ liệu khách hàng từ GitHub (Mac <-> Windows)...")
        ok, msg = sync_from_github(ROOT, fetch=True)
        if ok:
            try:
                import dashboard
                dashboard.generate(ROOT, save_leads=True)
            except Exception:
                pass
            print(f"✅ {msg}")
            return 0
        print(f"❌ {msg}")
        return 1

    print("🚀 Đang gộp dữ liệu Mac <-> Windows và đẩy bản cập nhật lên GitHub Pages...")
    ok, msg = deploy()
    if ok:
        print(f"🎉 {msg}")
        return 0
    else:
        print(f"❌ {msg}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
