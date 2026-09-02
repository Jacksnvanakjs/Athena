"""IR RSS 配置加载与展示时间格式。"""

import unittest

from app.deal_monitor.config import COMPANY_IR_FEEDS, _load_company_ir_feeds
from app.time_display import format_beijing_at_bj, format_beijing_at_et
from datetime import datetime


class TestCompanyIrFeeds(unittest.TestCase):
    def test_loads_adobe_google_news_feed(self):
        feeds = _load_company_ir_feeds()
        adbe = [f for f in feeds if f["ticker"] == "ADBE"]
        self.assertEqual(len(adbe), 1)
        self.assertEqual(adbe[0]["type"], "google_news")
        self.assertIn("news.adobe.com", adbe[0]["query"])

    def test_company_ir_feeds_non_empty(self):
        self.assertGreater(len(COMPANY_IR_FEEDS), 40)


class TestBeijingTimeDisplay(unittest.TestCase):
    def test_bj_et_split(self):
        dt = datetime(2026, 9, 1, 22, 45, 0)
        self.assertEqual(format_beijing_at_bj(dt), "2026-09-01 22:45")
        self.assertEqual(format_beijing_at_et(dt), "2026-09-01 10:45")


if __name__ == "__main__":
    unittest.main()
