"""币安公开行情源：符号映射 + 报价/日 K 解析。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.market_data.binance import (
    _row_from_24hr,
    to_binance_symbol,
)


class TestBinanceSymbolMap(unittest.TestCase):
    def test_common_aliases(self):
        self.assertEqual(to_binance_symbol("BTC"), "BTCUSDT")
        self.assertEqual(to_binance_symbol("btc-usd"), "BTCUSDT")
        self.assertEqual(to_binance_symbol("ETH"), "ETHUSDT")
        self.assertEqual(to_binance_symbol("SOLUSDT"), "SOLUSDT")
        self.assertIsNone(to_binance_symbol("NVDA"))
        self.assertIsNone(to_binance_symbol("COIN"))
        self.assertIsNone(to_binance_symbol(""))


class TestBinanceParse(unittest.TestCase):
    def test_24hr_row(self):
        row = _row_from_24hr(
            "BTC",
            {
                "symbol": "BTCUSDT",
                "lastPrice": "83597.10",
                "priceChangePercent": "0.241",
                "volume": "19420.5",
                "closeTime": 1_700_000_000_000,
            },
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["symbol"], "BTC")
        self.assertEqual(row["price"], 83597.1)
        self.assertEqual(row["change_pct"], 0.24)
        self.assertIn("quote_time", row)


class TestBinanceFetch(unittest.IsolatedAsyncioTestCase):
    async def test_quotes_batch(self):
        from app.market_data.binance import fetch_binance_quotes

        payload = [
            {
                "symbol": "BTCUSDT",
                "lastPrice": "100.0",
                "priceChangePercent": "1.5",
                "volume": "10",
                "closeTime": 1_700_000_000_000,
            },
            {
                "symbol": "ETHUSDT",
                "lastPrice": "200.0",
                "priceChangePercent": "-0.5",
                "volume": "20",
                "closeTime": 1_700_000_000_000,
            },
        ]
        with patch(
            "app.market_data.binance._get_json",
            new=AsyncMock(return_value=payload),
        ):
            out = await fetch_binance_quotes(["BTC", "ETH", "NVDA"])
        self.assertIn("BTC", out)
        self.assertIn("ETH", out)
        self.assertNotIn("NVDA", out)
        self.assertEqual(out["BTC"]["price"], 100.0)

    async def test_daily_closes(self):
        from app.market_data.binance import fetch_binance_daily_closes

        klines = [
            [1_700_000_000_000, "1", "2", "0.5", "1.5", "100", 1_700_086_399_999],
            [1_700_086_400_000, "1.5", "3", "1", "2.5", "200", 1_700_172_799_999],
            [1_700_172_800_000, "2.5", "4", "2", "3.5", "300", 1_700_259_199_999],
        ]
        with patch(
            "app.market_data.binance._get_json",
            new=AsyncMock(return_value=klines),
        ):
            rows = await fetch_binance_daily_closes("BTC", lookback_days=10)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1][1], 3.5)

        with patch(
            "app.market_data.binance._get_json",
            new=AsyncMock(return_value=klines),
        ):
            empty = await fetch_binance_daily_closes("NVDA", lookback_days=10)
        self.assertEqual(empty, [])


if __name__ == "__main__":
    unittest.main()
