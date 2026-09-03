"""通稿源配置、入库时效过滤。"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.deal_monitor.config import PR_WIRE_FEEDS
from app.deal_monitor.pipeline import (
    _published_too_stale_for_ingest,
    _published_too_stale_for_push,
)


class TestWireFeeds(unittest.TestCase):
    def test_includes_business_wire(self):
        names = {f["name"] for f in PR_WIRE_FEEDS}
        self.assertIn("business_wire", names)
        self.assertIn("business_wire_ma", names)
        urls = " ".join(f["url"] for f in PR_WIRE_FEEDS)
        self.assertIn("feed.businesswire.com", urls)


class TestIngestAge(unittest.TestCase):
    def test_fresh_ok(self):
        pub = datetime.now(timezone.utc) - timedelta(hours=6)
        self.assertFalse(_published_too_stale_for_ingest(pub))

    def test_old_dropped(self):
        pub = datetime.now(timezone.utc) - timedelta(days=10)
        with patch("app.deal_monitor.pipeline.DEAL_INGEST_MAX_AGE_DAYS", 3):
            self.assertTrue(_published_too_stale_for_ingest(pub))

    def test_naive_utc_ok(self):
        pub = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        self.assertFalse(_published_too_stale_for_ingest(pub))

    def test_push_stale_still_works(self):
        pub = datetime.now(timezone.utc) - timedelta(days=5)
        with patch("app.deal_monitor.pipeline.DEAL_PUSH_MAX_AGE_DAYS", 3):
            self.assertTrue(_published_too_stale_for_push(pub.replace(tzinfo=None)))


if __name__ == "__main__":
    unittest.main()
