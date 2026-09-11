"""主线/日线：东财优先与并发；宁缺勿错。"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from app.market_data.daily_closes import (
    _daily_close_sources,
    _parse_em_klines,
    fetch_daily_closes_many,
)


class TestDailyCloseSources(unittest.TestCase):
    def test_includes_rotating_vendors(self):
        names = [n for n, _ in _daily_close_sources("NVDA", 60, skip_yahoo=True)]
        self.assertIn("eastmoney", names)
        self.assertIn("alltick", names)
        self.assertIn("alpha_vantage", names)
        self.assertIn("twelve_data", names)
        self.assertNotIn("yahoo", names)

    def test_with_yahoo_included(self):
        names = [n for n, _ in _daily_close_sources("NVDA", 60, skip_yahoo=False)]
        self.assertIn("eastmoney", names)
        self.assertIn("yahoo", names)
        self.assertIn("polygon", names)


class TestEastMoneyKlineParse(unittest.TestCase):
    def test_parse_open_close_high_low_order(self):
        # 东财字段：date,open,close,high,low,...
        lines = [
            "2026-09-09,220.00,223.67,224.00,219.00,1,1,1",
            "2026-09-10,220.525,218.360,220.990,217.200,1,1,1",
        ]
        bars = _parse_em_klines(lines, lookback_days=40)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1][0], date(2026, 9, 10))
        self.assertEqual(bars[-1][1], 220.525)  # open
        self.assertEqual(bars[-1][4], 218.360)  # close
        self.assertEqual(bars[-1][2], 220.990)  # high
        self.assertEqual(bars[-1][3], 217.200)  # low


class TestFetchDailyClosesMany(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_skips_empty(self):
        async def fake_em(symbols, lookback_days=60, concurrency=5):
            return {"NVDA": [(date(2026, 9, 1), 10.0), (date(2026, 9, 2), 11.0)]}

        async def fake(sym, lookback_days=60, skip_yahoo=None):
            if sym == "BAD":
                return []
            return [(date(2026, 9, 1), 10.0), (date(2026, 9, 2), 11.0)]

        with (
            patch(
                "app.market_data.daily_closes.fetch_eastmoney_closes_many",
                new=AsyncMock(side_effect=fake_em),
            ),
            patch(
                "app.market_data.daily_closes.fetch_daily_closes",
                new=AsyncMock(side_effect=fake),
            ),
        ):
            out = await fetch_daily_closes_many(["NVDA", "BAD", "AMD"], concurrency=2)
        self.assertIn("NVDA", out)
        self.assertIn("AMD", out)
        self.assertNotIn("BAD", out)


class TestPeriodReturnsParallel(unittest.IsolatedAsyncioTestCase):
    async def test_period_merges_em_and_yahoo(self):
        import os

        from app.heatmap import fetch_period_returns

        os.environ.pop("HEATMAP_SKIP_YAHOO", None)
        with (
            patch(
                "app.heatmap._period_returns_from_snapshots",
                return_value={},
            ),
            patch(
                "app.heatmap._period_returns_from_daily_closes",
                new=AsyncMock(
                    return_value={"NVDA": {"ret_5d": 1.5, "ret_20d": None}}
                ),
            ),
            patch(
                "app.heatmap._period_returns_from_yahoo",
                new=AsyncMock(
                    return_value={"NVDA": {"ret_5d": None, "ret_20d": 3.0}}
                ),
            ),
        ):
            out = await fetch_period_returns(["NVDA"])
        self.assertEqual(out["NVDA"]["ret_5d"], 1.5)
        self.assertEqual(out["NVDA"]["ret_20d"], 3.0)


if __name__ == "__main__":
    unittest.main()
