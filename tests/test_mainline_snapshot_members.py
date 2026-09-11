"""主线快照组装应带上成分列表。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.ai_mainline.pipeline import _basket_members, _payload_from_db_snapshots


class TestBasketMembers(unittest.TestCase):
    def test_known_theme_has_symbols(self):
        # 用真实篮子里任意一条；若为空说明配置问题
        from app.ai_mainline.baskets import enabled_themes

        themes = enabled_themes()
        self.assertTrue(themes)
        key = themes[0]["key"]
        members = _basket_members(key)
        self.assertGreater(len(members), 0)
        self.assertIn("symbol", members[0])


class TestSnapshotIncludesMembers(unittest.TestCase):
    def test_snapshot_payload_fills_members(self):
        from app.ai_mainline.baskets import enabled_themes
        from app.ai_mainline.config import META_KEY

        themes = enabled_themes()
        self.assertTrue(themes)
        key = themes[0]["key"]

        theme_row = SimpleNamespace(
            theme_key=key,
            ret_1d=1.0,
            ret_5d=2.0,
            ret_20d=3.0,
            rel_1d=0.1,
            rel_5d=0.2,
            rel_20d=0.3,
            breadth=0.5,
            rank_5d=1,
            n_valid=4,
            payload_json='{"leaders":["NVDA"],"name":"测试"}',
        )
        meta_row = SimpleNamespace(
            theme_key=META_KEY,
            ret_1d=0.0,
            ret_5d=0.0,
            ret_20d=0.0,
            payload_json='{"primary_key":"%s","status":"emerging"}' % key,
        )

        mock_db = MagicMock()
        q = mock_db.query.return_value
        q.order_by.return_value.limit.return_value.scalar.return_value = "2026-09-10"
        q.filter.return_value.all.return_value = [theme_row, meta_row]

        with patch("app.database.SessionLocal", return_value=MagicMock(__enter__=lambda s: mock_db, __exit__=lambda *a: False)):
            # SessionLocal is imported inside the function from app.database
            pass

        with patch("app.ai_mainline.pipeline.SessionLocal", create=True):
            pass

        # patch where used: inside function `from app.database import ... SessionLocal`
        with patch("app.database.SessionLocal") as SL:
            SL.return_value.__enter__.return_value = mock_db
            SL.return_value.__exit__.return_value = False
            out = _payload_from_db_snapshots()

        self.assertIsNotNone(out)
        assert out is not None
        row = out["themes"][0]
        self.assertTrue(row.get("members"))
        self.assertTrue(row.get("tickers"))


if __name__ == "__main__":
    unittest.main()
