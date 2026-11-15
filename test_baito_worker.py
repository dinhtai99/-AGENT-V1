#!/usr/bin/env python3
"""Unit tests for AGENT-V1 Baito Japan Small Business Lead Finder."""

import csv
import tempfile
import unittest
from pathlib import Path

from browser_worker import _clean_profile_url, run_task
from filters import (
    analyze_business_and_needs,
    build_leads_list,
    classify_baito_post,
    extract_fields,
    parse_price_vnd,
    post_matches_filters,
    save_leads_csv,
)


class TestBaitoSmallBusinessLeadFinder(unittest.TestCase):
    def test_keep_vietnamese_restaurant_owner(self):
        text = (
            "Quán Phở Hà Nội tại ga Shin-Okubo (đi bộ 3 phút) cần tuyển gấp 2 bạn "
            "phụ bếp và chạy bàn ca tối (17h-23h) và cuối tuần. Lương 1200¥/h, "
            "hỗ trợ tàu xe, bao ăn tại quán. Quán mình mới mở thêm chi nhánh 2 nên "
            "cần bạn làm lâu dài, có hỗ trợ chuyển visa Tokutei. "
            "LH SĐT: ０８０ー１２３４ー５６７８ hoặc Line: thaopho88"
        )
        info = analyze_business_and_needs(text)
        self.assertTrue(info["is_target_owner"])
        self.assertEqual(info["post_category"], "small_business_owner")
        self.assertEqual(info["business_type"], "Nhà hàng / Quán ăn Việt Nam")
        self.assertEqual(info["shop_name"], "Phở Hà Nội")
        self.assertEqual(info["headcount_mentioned"], 2)
        self.assertIn("Shin-Okubo", info["stations"])
        self.assertEqual(info["contact"]["phone"], "080-1234-5678")
        self.assertEqual(info["contact"]["line_id"], "thaopho88")
        self.assertEqual(info["wage"], "1200¥/h")
        self.assertIn("Bếp / Phụ bếp (Kitchen)", info["roles_needed"])
        self.assertIn("Phục vụ / Chạy bàn (Hall)", info["roles_needed"])
        # Deep needs check
        needs_joined = " | ".join(info["pain_points_and_needs"])
        self.assertIn("khẩn cấp", needs_joined)
        self.assertIn("mở rộng", needs_joined)
        self.assertIn("gắn bó lâu dài", needs_joined)

    def test_keep_nail_salon_owner_and_infer_japanese_customers(self):
        text = (
            "Tiệm Nail Hana ở ga Ikebukuro cần tuyển 1 bạn thợ nail và nối mi "
            "làm cuối tuần. Lương 1,250 yên/giờ + thưởng. Yêu cầu tiếng Nhật N2 "
            "giao tiếp tốt vì khách của tiệm đều là người Nhật. "
            "Zalo: 0912.345.678 / ĐT: 090-8765-4321"
        )
        info = analyze_business_and_needs(text)
        self.assertTrue(info["is_target_owner"])
        self.assertEqual(info["business_type"], "Tiệm Nail / Mi / Spa / Làm đẹp")
        self.assertIn("Thợ Nail / Nối mi / Spa", info["roles_needed"])
        self.assertEqual(info["shop_customer_segment"], "Phục vụ khách bản địa Nhật Bản")
        self.assertEqual(info["contact"]["phone"], "090-8765-4321")
        self.assertEqual(info["contact"]["zalo_id"], "0912.345.678")

    def test_reject_job_seeker_post(self):
        text = (
            "Xin phép ad ạ. Em mới sang Nhật được 2 tháng, hiện đang ở gần ga Otsuka. "
            "Em cần tìm baito quán ăn hoặc xưởng cơm hộp ca chiều tối ạ. "
            "Anh chị chủ quán nào cần người chấm bài giúp em với ạ!"
        )
        cls = classify_baito_post(text)
        self.assertFalse(cls["is_target_owner"])
        self.assertEqual(cls["category"], "job_seeker")

    def test_reject_large_haken_or_over_20_headcount(self):
        text_haken = (
            "Công ty phái cử Haken Tokyo thông báo tuyển 35 nhân viên đóng gói "
            "cơm hộp tại Saitama, lương 1150¥/h, có xe đưa đón."
        )
        cls1 = classify_baito_post(text_haken)
        self.assertFalse(cls1["is_target_owner"])
        self.assertEqual(cls1["category"], "agency_or_large")

        text_big_count = (
            "Xưởng thực phẩm cần tuyển gấp 25 bạn làm ca đêm tại Chiba, "
            "lương 1300 yên/h."
        )
        cls2 = classify_baito_post(text_big_count)
        self.assertFalse(cls2["is_target_owner"])
        self.assertEqual(cls2["category"], "agency_or_large")

    def test_post_matches_filters_for_baito_task(self):
        task = {
            "only_small_business": True,
            "customer_profile": "Cá nhân kinh doanh nhỏ hoặc chủ mô hình kinh doanh nhỏ dưới 20 người tại Nhật Bản",
        }
        owner_post = {
            "post_id": "1",
            "content": "Quán nhậu Izakaya Sakura ở Namba Osaka cần tuyển 2 bạn chạy bàn và phụ bếp, lương 1150¥/h, bao ăn.",
            "extracted": extract_fields(
                "Quán nhậu Izakaya Sakura ở Namba Osaka cần tuyển 2 bạn chạy bàn và phụ bếp, lương 1150¥/h, bao ăn.",
                ["phone", "business_type", "location", "wage"],
            ),
        }
        seeker_post = {
            "post_id": "2",
            "content": "Em cần tìm việc làm thêm quanh ga Namba Osaka các ngày trong tuần ạ.",
            "extracted": {},
        }
        self.assertTrue(post_matches_filters(owner_post, task))
        self.assertFalse(post_matches_filters(seeker_post, task))

    def test_lead_deduplication_and_csv_export(self):
        posts = [
            {
                "post_id": "101",
                "post_url": "https://www.facebook.com/groups/baito1/posts/101/",
                "group_id": "baito1",
                "author_name": "Trần Minh (Quán Việt)",
                "author_url": "https://www.facebook.com/profile.php?id=1000555",
                "group_member_url": "https://www.facebook.com/groups/baito1/user/1000555/",
                "content": "Quán Bún Chả Hà Nội ở ga Ueno cần tuyển 1 bạn phụ bếp ca sáng. Lương 1200¥/h.",
            },
            {
                "post_id": "102",
                "post_url": "https://www.facebook.com/groups/baito2/posts/102/",
                "group_id": "baito2",
                "author_name": "Trần Minh (Quán Việt)",
                "author_url": "https://www.facebook.com/profile.php?id=1000555",
                "group_member_url": "https://www.facebook.com/groups/baito2/user/1000555/",
                "content": "Quán mình ở ga Ueno cần tuyển gấp thêm 1 bạn chạy bàn ca tối. SĐT: 080-9999-8888, Line: minh_ueno",
            },
        ]
        leads = build_leads_list(posts)
        self.assertEqual(len(leads), 1)
        lead = leads[0]
        self.assertEqual(lead["personal_info"]["author_name"], "Trần Minh (Quán Việt)")
        self.assertEqual(lead["personal_info"]["phone"], "080-9999-8888")
        self.assertEqual(lead["personal_info"]["line_id"], "minh_ueno")
        self.assertEqual(lead["step1_business_model"]["business_type"], "Nhà hàng / Quán ăn Việt Nam")
        self.assertEqual(len(lead["source_posts"]), 2)
        self.assertIn("Bếp / Phụ bếp (Kitchen)", lead["step2_deep_needs"]["roles_needed"])
        self.assertIn("Phục vụ / Chạy bàn (Hall)", lead["step2_deep_needs"]["roles_needed"])

        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "leads.csv"
            save_leads_csv(csv_path, leads)
            with csv_path.open("r", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)
            self.assertIn("Trần Minh (Quán Việt)", rows[1][0])
            self.assertIn("080-9999-8888", rows[1][3])

    def test_parse_price_vnd_and_jp_units(self):
        self.assertEqual(parse_price_vnd("100.000đ"), 100_000)
        self.assertEqual(parse_price_vnd("1.5 triệu"), 1_500_000)
        self.assertEqual(parse_price_vnd("1,200¥"), 1200)
        self.assertEqual(parse_price_vnd("25 man"), 250_000)

    def test_clean_profile_url(self):
        author_url, member_url = _clean_profile_url(
            "/groups/baito.tokyo/user/100012345/?__cft__[0]=abc", "baito.tokyo"
        )
        self.assertEqual(author_url, "https://www.facebook.com/profile.php?id=100012345")
        self.assertEqual(member_url, "https://www.facebook.com/groups/baito.tokyo/user/100012345/")

    def test_demo_mode_end_to_end(self):
        root = Path(__file__).parent
        json_path = root / "leads.json"
        csv_path = root / "leads.csv"
        old_json = json_path.read_bytes() if json_path.exists() else None
        old_csv = csv_path.read_bytes() if csv_path.exists() else None
        try:
            json_path.unlink(missing_ok=True)
            csv_path.unlink(missing_ok=True)
            res = run_task(
                {"task_id": "TEST-DEMO", "action": "search_group_posts"},
                Path("fb-state.json"),
                demo=True,
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(len(res["leads"]), 2)
        finally:
            if old_json is not None:
                json_path.write_bytes(old_json)
            else:
                json_path.unlink(missing_ok=True)
            if old_csv is not None:
                csv_path.write_bytes(old_csv)
            else:
                csv_path.unlink(missing_ok=True)

    def test_dashboard_generation(self):
        from dashboard import build, render_html
        root = Path(__file__).parent
        data, leads = build(root)
        self.assertGreaterEqual(len(data["leads"]), 1)
        self.assertGreaterEqual(len(data["raw_posts"]), 1)
        html = render_html(root, data)
        self.assertIn("<!doctype html>", html)
        self.assertIn('id="dashboard-data"', html)
        self.assertIn("Baito Lead Finder", html)


if __name__ == "__main__":
    unittest.main()
