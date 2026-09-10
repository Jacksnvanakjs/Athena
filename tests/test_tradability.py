"""美股可交易性：退市/无近期日线不可入库。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

from app.market_data.tradability import is_us_tradable


class TestUsTradable(unittest.IsolatedAsyncioTestCase):
    async def test_recent_close_ok(self):
        today = date(2026, 9, 10)
        closes = [(today - timedelta(days=1), 10.0)]
        with patch(
            "app.market_data.tradability.fetch_daily_closes",
            new=AsyncMock(return_value=closes),
        ):
            self.assertTrue(await is_us_tradable("META", as_of=today))

    async def test_stale_delisted_rejected(self):
        today = date(2026, 9, 10)
        closes = [(date(2025, 2, 12), 3.14)]
        with patch(
            "app.market_data.tradability.fetch_daily_closes",
            new=AsyncMock(return_value=closes),
        ):
            self.assertFalse(await is_us_tradable("CTV", as_of=today))

    async def test_empty_closes_rejected(self):
        with patch(
            "app.market_data.tradability.fetch_daily_closes",
            new=AsyncMock(return_value=[]),
        ):
            self.assertFalse(await is_us_tradable("XXXX"))

    async def test_blank_ticker_rejected(self):
        self.assertFalse(await is_us_tradable(""))


if __name__ == "__main__":
    unittest.main()
