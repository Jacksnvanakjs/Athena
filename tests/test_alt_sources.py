"""TradingView / Finviz / 轮动级联解析测试。"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.market_data.alt_sources import _parse_finviz_snapshot, _to_float
from app.market_data.cascade import next_batch_order, rotate_providers


class TestFinvizParse(unittest.TestCase):
    def test_price_prev_close_pairs(self):
        html = """
        <td class="snapshot-td2">Prev Close</td><td class="snapshot-td2">223.67</td>
        <td class="snapshot-td2">Price</td><td class="snapshot-td2">218.36</td>
        <td class="snapshot-td2">Volume</td><td class="snapshot-td2">105,768,001</td>
        """
        snap = _parse_finviz_snapshot(html)
        self.assertEqual(snap["Price"], "218.36")
        self.assertEqual(snap["Prev Close"], "223.67")
        self.assertEqual(_to_float(snap["Volume"]), 105768001.0)


class TestRotate(unittest.TestCase):
    def test_batch_order_rotates(self):
        a = next_batch_order("test_rotate_unit", ["A", "B", "C"])
        b = next_batch_order("test_rotate_unit", ["A", "B", "C"])
        self.assertEqual(len(a), 3)
        self.assertEqual(set(a), {"A", "B", "C"})
        # 连续两次起点应不同（除非只有 1 个）
        self.assertNotEqual(a[0], b[0])


class TestTradingViewMock(unittest.IsolatedAsyncioTestCase):
    async def test_parse_tv_payload(self):
        from app.market_data.alt_sources import fetch_tradingview_quotes

        class Resp:
            status_code = 200

            def json(self):
                return {
                    "data": [
                        {
                            "s": "NASDAQ:NVDA",
                            "d": [218.36, -2.37, -5.31, 105767884],
                        }
                    ]
                }

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, *args, **kwargs):
                return Resp()

        with patch("app.market_data.alt_sources.httpx.AsyncClient", return_value=Client()):
            out = await fetch_tradingview_quotes(["NVDA"])
        self.assertIn("NVDA", out)
        self.assertEqual(out["NVDA"]["price"], 218.36)
        self.assertEqual(out["NVDA"]["change_pct"], -2.37)


if __name__ == "__main__":
    unittest.main()
