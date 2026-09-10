"""主线/日线：跳过 Yahoo 时的源顺序与并发；宁缺勿错。"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app.market_data.daily_closes import _daily_close_sources, fetch_daily_closes_many


class TestDailyCloseSources(unittest.TestCase):
    def test_skip_yahoo_prefers_stooq(self):
        names = [n for n, _ in _daily_close_sources("NVDA", 60, skip_yahoo=True)]
        self.assertEqual(names[0], "stooq")
        self.assertNotIn("yahoo", names)

    def test_with_yahoo_first(self):
        names = [n for n, _ in _daily_close_sources("NVDA", 60, skip_yahoo=False)]
        self.assertEqual(names[0], "yahoo")
        self.assertIn("stooq", names)


class TestFetchDailyClosesMany(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_skips_empty(self):
        async def fake(sym, lookback_days=60, skip_yahoo=None):
            if sym == "BAD":
                return []
            return [(date(2026, 9, 1), 10.0), (date(2026, 9, 2), 11.0)]

        with patch(
            "app.market_data.daily_closes.fetch_daily_closes",
            new=AsyncMock(side_effect=fake),
        ):
            out = await fetch_daily_closes_many(["NVDA", "BAD", "AMD"], concurrency=2)
        self.assertIn("NVDA", out)
        self.assertIn("AMD", out)
        self.assertNotIn("BAD", out)


class TestPeriodReturnsSkipYahoo(unittest.IsolatedAsyncioTestCase):
    async def test_skip_yahoo_does_not_call_yahoo_chart(self):
        import os

        from app.heatmap import fetch_period_returns

        os.environ["HEATMAP_SKIP_YAHOO"] = "1"
        try:
            with (
                patch(
                    "app.heatmap._period_returns_from_snapshots",
                    return_value={},
                ),
                patch(
                    "app.heatmap._period_returns_from_yahoo",
                    new=AsyncMock(return_value={"NVDA": {"ret_5d": 99.0, "ret_20d": 99.0}}),
                ) as yahoo,
                patch(
                    "app.heatmap._period_returns_from_daily_closes",
                    new=AsyncMock(
                        return_value={"NVDA": {"ret_5d": 1.5, "ret_20d": 3.0}}
                    ),
                ),
            ):
                out = await fetch_period_returns(["NVDA"])
            yahoo.assert_not_awaited()
            self.assertEqual(out["NVDA"]["ret_5d"], 1.5)
        finally:
            os.environ.pop("HEATMAP_SKIP_YAHOO", None)


if __name__ == "__main__":
    unittest.main()
