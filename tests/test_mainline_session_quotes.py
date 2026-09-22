"""Yahoo 会话价解析：休市应优先盘后价。"""

from app.heatmap import _parse_yahoo_meta


def test_yahoo_closed_prefers_post_market():
    meta = {
        "marketState": "CLOSED",
        "previousClose": 100.0,
        "regularMarketPrice": 105.0,
        "regularMarketChangePercent": 5.0,
        "postMarketPrice": 98.0,
        "postMarketChangePercent": -2.0,
        "postMarketTime": 1_700_000_000,
        "shortName": "TEST",
    }
    row = _parse_yahoo_meta(meta, None, "TEST")
    assert row is not None
    assert row["price"] == 98.0
    assert row["change_pct"] == -2.0


def test_yahoo_pre_uses_pre_market():
    meta = {
        "marketState": "PRE",
        "previousClose": 100.0,
        "regularMarketPrice": 105.0,
        "regularMarketChangePercent": 5.0,
        "preMarketPrice": 97.5,
        "preMarketChangePercent": -2.5,
        "shortName": "TEST",
    }
    row = _parse_yahoo_meta(meta, None, "TEST")
    assert row is not None
    assert row["price"] == 97.5
    assert abs(row["change_pct"] + 2.5) < 1e-6


def test_live_1d_active_phases():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.ai_mainline.pipeline import _live_1d_active, _market_phase

    assert _live_1d_active("pre_open")
    assert _live_1d_active("rth")
    assert _live_1d_active("settle")
    assert _live_1d_active("overnight")
    assert not _live_1d_active("closed")

    et = ZoneInfo("America/New_York")
    # 周日 20:00 ET → 隔夜盘，应继续拉 1D
    sunday_night = datetime(2026, 9, 20, 20, 5, tzinfo=et)  # Sunday
    assert _market_phase(sunday_night) == "overnight"
    assert _live_1d_active(_market_phase(sunday_night))
    # 周六白天 → 休市不拉
    saturday = datetime(2026, 9, 19, 15, 0, tzinfo=et)
    assert _market_phase(saturday) == "closed"
    assert not _live_1d_active(_market_phase(saturday))
