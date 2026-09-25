"""Yahoo 会话价解析：休市优先盘后价作公共回退。"""

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


def test_data_times_from_quotes_and_trade_date():
    from datetime import date

    from app.ai_mainline.pipeline import _daily_session_close_times, _quote_data_times

    times = _quote_data_times(
        {
            "A": {"quote_time": "2026-09-22 21:10:00", "quote_time_et": "2026-09-22 09:10:00 EDT"},
            "B": {"quote_time": "2026-09-22 21:15:00", "quote_time_et": "2026-09-22 09:15:00 EDT"},
        }
    )
    assert times["data_time_1d_bj"] == "2026-09-22 21:15:00"
    assert "09:15" in (times["data_time_1d_et"] or "")

    daily = _daily_session_close_times(date(2026, 9, 19))
    assert daily["data_time_daily_bj"]  # 夏令时 16:00 ET = 04:00+1 BJ
    assert "16:00" in (daily["data_time_daily_et"] or "")


def test_live_1d_active_phases():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.ai_mainline.pipeline import (
        _live_1d_active,
        _market_phase,
        _overlay_interval_sec,
    )

    assert _live_1d_active("pre_open")
    assert _live_1d_active("rth")
    assert _live_1d_active("settle")
    assert _live_1d_active("overnight")
    assert not _live_1d_active("closed")
    assert _overlay_interval_sec("rth") == 45.0
    assert _overlay_interval_sec("overnight") == 90.0

    et = ZoneInfo("America/New_York")
    # 周日 20:00 ET → 夜盘
    sunday_night = datetime(2026, 9, 20, 20, 5, tzinfo=et)
    assert _market_phase(sunday_night) == "overnight"
    assert _live_1d_active(_market_phase(sunday_night))

    # 交易日凌晨 02:30 = 夜盘 ATS（不是盘前）
    thu_owl = datetime(2026, 9, 25, 2, 30, tzinfo=et)
    assert _market_phase(thu_owl) == "overnight"

    # 盘后 17:00 = settle
    thu_post = datetime(2026, 9, 24, 17, 0, tzinfo=et)
    assert _market_phase(thu_post) == "settle"

    # 盘前 05:00
    thu_pre = datetime(2026, 9, 25, 5, 0, tzinfo=et)
    assert _market_phase(thu_pre) == "pre_open"
