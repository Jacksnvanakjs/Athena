"""新浪行情字段：盘中勿用滞后盘前价。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.heatmap import _parse_sina_row

_ET = ZoneInfo("America/New_York")


def _parts(
    price: str,
    pct: str,
    *,
    ext: str,
    prev: str,
    et: str,
) -> list[str]:
    row = [""] * 36
    row[0] = "特斯拉"
    row[1] = price
    row[2] = pct
    row[10] = "50000000"
    row[21] = ext
    row[24] = et
    row[26] = prev
    return row


def test_regular_session_uses_latest_price_not_stale_premarket():
    # 盘中 10:00 ET：最新价 340（跌），[21] 仍停在盘前 362（涨）
    parts = _parts(
        "340.00",
        "-2.50",
        ext="362.8500",
        prev="348.7500",
        et="Sep 01 10:00AM EDT",
    )
    q = _parse_sina_row(parts, "TSLA")
    assert q is not None
    assert q["price"] == 340.0
    assert q["change_pct"] == round((340 - 348.75) / 348.75 * 100, 2)


def test_premarket_uses_extended_price():
    parts = _parts(
        "367.95",
        "5.51",
        ext="362.8500",
        prev="348.7500",
        et="Sep 01 05:08AM EDT",
    )
    q = _parse_sina_row(parts, "TSLA")
    assert q is not None
    assert q["price"] == 362.85
    assert q["change_pct"] == round((362.85 - 348.75) / 348.75 * 100, 2)
