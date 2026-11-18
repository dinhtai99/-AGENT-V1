"""Unit tests for deep lead behavior profiling, Google Maps classification, and 1-10 scoring."""

import unittest

from filters import analyze_lead_behavior, classify_website_url, extract_shop_name, score_lead_web_priority
from lead_profiler import build_maps_search_queries


class TestLeadProfilerAndScoring(unittest.TestCase):
    def test_classify_website_url(self):
        self.assertEqual(classify_website_url(""), "NO_WEB")
        self.assertEqual(classify_website_url(None), "NO_WEB")
        self.assertEqual(classify_website_url("https://www.facebook.com/tiemnailx"), "SOCIAL_ONLY")
        self.assertEqual(classify_website_url("https://www.instagram.com/my_salon/"), "SOCIAL_ONLY")
        self.assertEqual(classify_website_url("https://beauty.hotpepper.jp/kr/slnH000123/"), "SOCIAL_ONLY")
        self.assertEqual(classify_website_url("https://lin.ee/abc123"), "SOCIAL_ONLY")
        self.assertEqual(classify_website_url("https://sakura-nail-tokyo.jp/"), "HAS_WEB")

    def test_analyze_lead_behavior_across_multiple_posts(self):
        posts = [
            {
                "post_id": "p1",
                "created_at": "2026-10-01T22:15:00",
                "content": "Tiệm Nail Hoa Hồng gần ga Shin-Okubo tuyển thợ nail cứng, bao lương.",
            },
            {
                "post_id": "p2",
                "created_at": "2026-10-15T22:45:00",
                "content": "Ưu đãi giảm giá 20% nối mi tại Tiệm Nail Hoa Hồng, 〒169-0073 新宿区百人町1-2-3. SĐT 080-1111-2222 IG: https://instagram.com/nailhoahong",
            },
        ]
        lead = {
            "personal_info": {"author_name": "Lan Anh", "author_url": "https://www.facebook.com/profile.php?id=1001"},
            "step1_business_model": {"business_type": "Tiệm Nail / Mi / Spa", "location": "Tokyo"},
            "step2_deep_needs": {"roles_needed": ["Thợ Nail"]},
        }
        beh = analyze_lead_behavior(posts, lead)
        self.assertEqual(beh["total_posts"], 2)
        self.assertEqual(beh["golden_hour"], "Đêm khuya (21h-5h)")
        self.assertIn("080-1111-2222", beh["phones"])
        self.assertTrue(any("169-0073" in a for a in beh["address_clues"]))
        self.assertTrue(any("instagram.com" in s for s in beh["social_links"]))

    def test_score_1_to_10_preliminary_vs_verified_maps(self):
        lead = {
            "personal_info": {
                "author_name": "Lan Anh",
                "author_url": "https://www.facebook.com/profile.php?id=1001",
                "phone": "080-1111-2222",
                "all_phones": ["080-1111-2222"],
            },
            "step1_business_model": {
                "business_type": "Tiệm Nail / Mi / Spa",
                "shop_name": "Tiệm Nail Hoa Hồng",
                "location": "Shinjuku, Tokyo",
                "stations": ["Shin-Okubo"],
            },
            "step2_deep_needs": {"roles_needed": ["Thợ Nail"]},
            "source_posts": [
                {"post_id": "1", "created_at": "2026-11-01T21:00:00", "content": "Tuyển thợ nail ga Shin-Okubo 080-1111-2222"}
            ],
        }
        beh = analyze_lead_behavior(lead["source_posts"], lead)

        # Preliminary (no Maps check yet) should be capped <= 7
        prelim = score_lead_web_priority(lead, beh, None)
        self.assertTrue(prelim["is_preliminary"])
        self.assertLessEqual(prelim["score"], 7)

        # Verified on Maps with good reviews + NO_WEB -> should be 9 or 10 (Tier S)
        maps_no_web = {
            "status": "FOUND",
            "place_name": "Hoa Hong Nail Salon",
            "rating": 4.7,
            "review_count": 32,
            "website": "",
            "website_status": "NO_WEB",
        }
        res_s = score_lead_web_priority(lead, beh, maps_no_web)
        self.assertFalse(res_s["is_preliminary"])
        self.assertGreaterEqual(res_s["score"], 9)
        self.assertEqual(res_s["tier"], "S")
        self.assertIn("Google Maps", res_s["web_opener"])

        # Verified on Maps with strong own website -> lower priority score
        maps_strong = {
            "status": "FOUND",
            "place_name": "Hoa Hong Nail Salon",
            "rating": 4.7,
            "review_count": 32,
            "website": "https://hoahongnail.jp",
            "website_status": "HAS_WEB",
        }
        res_has = score_lead_web_priority(lead, beh, maps_strong)
        self.assertLess(res_has["score"], res_s["score"])

    def test_build_maps_search_queries(self):
        lead = {
            "personal_info": {"phone": "080-1111-2222", "all_phones": ["080-1111-2222"]},
            "step1_business_model": {"shop_name": "Phở Hà Nội", "location": "Ikebukuro"},
        }
        beh = {"shop_names": ["Phở Hà Nội"], "phones": ["080-1111-2222"], "address_clues": []}
        qs = build_maps_search_queries(lead, beh)
        self.assertTrue(any("Phở Hà Nội Ikebukuro" == q for q, _ in qs))
        self.assertTrue(any("080-1111-2222" == q for q, _ in qs))

    def test_extract_shop_name_variants(self):
        self.assertEqual(extract_shop_name("678Quán chúc tất cả ACE ngày mới tốt lành"), "678Quán")
        self.assertEqual(extract_shop_name("Ghé JAPAN TRAVEL lấy vé về thăm gia đình"), "JAPAN TRAVEL")
        self.assertEqual(extract_shop_name("Linh Hiền Eyelashes kawaguchi Chuyên nối mi"), "Linh Hiền Eyelashes")
        self.assertEqual(extract_shop_name("Tuyển mẫu free design ga ikebukuro", author_name="Queens Nail"), "Queens Nail")


if __name__ == "__main__":
    unittest.main()
