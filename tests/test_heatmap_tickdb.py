"""TickDB 行情解析。"""

import unittest

from app.heatmap import _parse_tickdb_ticker, _symbol_from_tickdb, _tickdb_us_symbol


class TestTickdbParse(unittest.TestCase):
    def test_tickdb_symbol_mapping(self):
        self.assertEqual(_tickdb_us_symbol("aapl"), "AAPL.US")
        self.assertEqual(_symbol_from_tickdb("TSLA.US"), "TSLA")

    def test_parse_tickdb_ticker_fields(self):
        item = {
            "symbol": "AAPL.US",
            "name": "Apple",
            "last_price": "260.81",
            "volume_24h": "26218927",
            "price_change_percent_24h": "-0.01",
            "timestamp": 1773259201000,
        }
        q = _parse_tickdb_ticker(item, "AAPL")
        self.assertIsNotNone(q)
        self.assertEqual(q["symbol"], "AAPL")
        self.assertEqual(q["price"], 260.81)
        self.assertEqual(q["change_pct"], -0.01)
        self.assertEqual(q["volume"], 26218927)
        self.assertNotEqual(q["flow_score"], 0)


if __name__ == "__main__":
    unittest.main()
