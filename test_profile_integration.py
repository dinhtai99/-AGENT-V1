"""Customer profile must drive both the scan filter and saved dashboard view."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import dashboard
from filters import extract_fields, parse_customer_profile, post_matches_filters, save_json


POSTS = [
    {
        "post_id": "101", "group_id": "test", "author_name": "Anh Minh",
        "author_url": "https://www.facebook.com/profile.php?id=101",
        "content": "Quán ăn Việt tại Tokyo cần tuyển 2 bạn phụ bếp ca tối.",
    },
    {
        "post_id": "102", "group_id": "test", "author_name": "Chị Lan",
        "author_url": "https://www.facebook.com/profile.php?id=102",
        "content": "Tiệm nail ở Tokyo cần tuyển 1 bạn thợ làm móng.",
    },
    {
        "post_id": "103", "group_id": "test", "author_name": "Anh Hòa",
        "author_url": "https://www.facebook.com/profile.php?id=103",
        "content": "Quán ăn Việt ở Osaka cần tuyển 2 bạn phục vụ.",
    },
]


class CustomerProfileIntegrationTest(unittest.TestCase):
    def test_broad_profile_treats_parenthesized_sectors_as_examples(self):
        profile = "Cá nhân kinh doanh nhỏ dưới 20 người tại Nhật Bản (quán ăn, tiệm nail, shop)"
        self.assertNotIn("business_types", parse_customer_profile(profile))
        self.assertIn("business_types", parse_customer_profile("Chủ quán ăn Việt dưới 20 người tại Tokyo"))

    def test_worker_and_dashboard_agree_and_crm_survives_profile_change(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copyfile(Path(dashboard.__file__).with_name("dashboard_template.html"), root / "dashboard_template.html")
            task = {"only_small_business": True, "customer_profile": "Chủ quán ăn Việt dưới 20 người tại Tokyo"}
            save_json(root / "task-baito-nhom-da-tham-gia.json", task)
            save_json(root / "result.json", {"groups": [{"id": "test", "name": "Baito Nhật Bản"}], "posts": POSTS})

            decisions = []
            for original in POSTS:
                post = dict(original)
                post["group_name"] = "Baito Nhật Bản"
                post["extracted"] = extract_fields(post["content"], ["location", "level", "time", "price", "wage"])
                decisions.append(post_matches_filters(post, task))
            self.assertEqual(decisions, [True, False, False])
            misplaced = dict(POSTS[2], group_name="Tokyo baito")
            misplaced["extracted"] = extract_fields(misplaced["content"], ["location"], group_name="Tokyo baito")
            self.assertFalse(post_matches_filters(misplaced, task))

            data, leads = dashboard.build(root)
            self.assertEqual([row["is_lead"] for row in data["raw_posts"] if row["post_id"] == "101"], [True])
            self.assertEqual(len(data["leads"]), 1)
            self.assertEqual(len(leads), 1)
            leads[0]["crm"] = {"description": "Khách đã liên hệ", "status": "contacted"}
            save_json(root / "leads.json", {"leads": leads})

            task["customer_profile"] = "Chủ tiệm nail dưới 20 người tại Tokyo"
            save_json(root / "task-baito-nhom-da-tham-gia.json", task)
            dashboard.generate(root, save_leads=True)
            refreshed, _ = dashboard.build(root)
            self.assertEqual(len(refreshed["leads"]), 1)
            self.assertEqual(refreshed["leads"][0]["author_name"], "Chị Lan")
            archive = json.loads((root / "leads.json").read_text(encoding="utf-8"))["leads"]
            old = next(lead for lead in archive if lead["lead_key"] == leads[0]["lead_key"])
            self.assertEqual(old["crm"]["description"], "Khách đã liên hệ")


if __name__ == "__main__":
    unittest.main()
