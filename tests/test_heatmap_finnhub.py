"""Finnhub /quote 解析：用昨收重算涨跌幅。"""

import unittest

from app.heatmap import _parse_finnhub_quote


class TestFinnhubQuoteParse(unittest.TestCase):
    def test_recompute_change_from_prev_close(self):
        # 官方样例口径：c 现价、pc 昨收；dp 可能有四舍五入
        raw = {
            "c": 218.36,
            "d": -5.31,
            "dp": -2.374,
            "h": 220.99,
            "l": 217.2,
            "o": 220.525,
            "pc": 223.67,
            "t": 1789070400,
        }
        q = _parse_finnhub_quote(raw, "NVDA")
        self.assertIsNotNone(q)
        assert q is not None
        self.assertEqual(q["symbol"], "NVDA")
        self.assertEqual(q["price"], 218.36)
        expected = round((218.36 - 223.67) / 223.67 * 100, 2)
        self.assertEqual(q["change_pct"], expected)
        self.assertEqual(q["volume"], 0)

    def test_reject_zero_price(self):
        self.assertIsNone(
            _parse_finnhub_quote(
                {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0},
                "XXXX",
            )
        )

    def test_reject_missing_price(self):
        self.assertIsNone(_parse_finnhub_quote({"pc": 10}, "AAPL"))


if __name__ == "__main__":
    unittest.main()
