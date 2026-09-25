"""新浪行情字段：盘中勿用滞后盘前价；夜盘窗回退盘后扩展价。"""

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
    reg_et: str = "",
) -> list[str]:
    row = [""] * 36
    row[0] = "特斯拉"
    row[1] = price
    row[2] = pct
    row[10] = "50000000"
    row[21] = ext
    row[24] = et
    row[25] = reg_et
    row[26] = prev
    return row


def test_regular_session_uses_latest_price_not_stale_premarket():
    parts = _parts(
        "340.00",
        "-2.50",
        ext="362.8500",
        prev="348.7500",
        et="Sep 01 10:00AM EDT",
    )
    q = _parse_sina_row(
        parts, "TSLA", now_et=datetime(2026, 9, 1, 10, 0, tzinfo=_ET)
    )
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
    q = _parse_sina_row(
        parts, "TSLA", now_et=datetime(2026, 9, 1, 5, 8, tzinfo=_ET)
    )
    assert q is not None
    assert q["price"] == 362.85
    assert q["change_pct"] == round((362.85 - 348.75) / 348.75 * 100, 2)


def test_overnight_window_falls_back_to_post_extended():
    # 公共源无 ATS：夜盘窗回退盘后扩展价
    parts = _parts(
        "224.58",
        "-0.41",
        ext="223.8200",
        prev="225.50",
        et="Sep 24 07:59PM EDT",
        reg_et="Sep 24 04:00PM EDT",
    )
    q = _parse_sina_row(
        parts, "NVDA", now_et=datetime(2026, 9, 25, 2, 30, tzinfo=_ET)
    )
    assert q is not None
    assert q["price"] == 223.82
    assert q["change_pct"] == round((223.82 - 225.50) / 225.50 * 100, 2)
    assert q.get("quote_time_et") and "07:59" in q["quote_time_et"]
