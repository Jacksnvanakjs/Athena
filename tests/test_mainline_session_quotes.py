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
        _near_phase_switch,
        _needs_1d_refresh,
        _overlay_interval_sec,
    )

    assert _live_1d_active("pre_open")
    assert _live_1d_active("rth")
    assert _live_1d_active("settle")
    assert _live_1d_active("overnight")
    assert not _live_1d_active("closed")
    assert _overlay_interval_sec("rth") == 45.0 or _near_phase_switch()
    assert _overlay_interval_sec("pre_open") in (30.0, 45.0)
    assert _overlay_interval_sec("overnight") in (30.0, 90.0)

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

    # 切换点附近
    assert _near_phase_switch(datetime(2026, 9, 25, 3, 55, tzinfo=et))
    assert _near_phase_switch(datetime(2026, 9, 25, 9, 28, tzinfo=et))
    assert not _near_phase_switch(datetime(2026, 9, 25, 12, 0, tzinfo=et))

    # 盘前还挂昨收 → 必须重拉
    need, reason = _needs_1d_refresh(
        {
            "live_1d": True,
            "session_phase": "overnight",
            "data_time_1d_bj": "2026-09-25 04:00:00",
            "data_time_1d_et": "2026-09-24 16:00:00 EDT",
        },
        "pre_open",
        quote_age=10.0,
    )
    assert need
    assert reason.startswith("phase_switch") or reason == "stamp_mismatch"


def test_overnight_keeps_rth_holes_when_session_partial():
    from app.ai_mainline.pipeline import _filter_quotes_for_1d

    quotes = {
        "GLW": {
            "change_pct": 5.8,
            "quote_source": "cnbc",
            "quote_time_et": "2026-10-06 19:59:00 EDT",
            "quote_time": "2026-10-07 07:59:00",
        },
        "ORCL": {
            "change_pct": 1.6,
            "quote_source": "eastmoney",
            "quote_time_et": "2026-10-06 16:00:00 EDT",
            "quote_time": "2026-10-07 04:00:00",
        },
    }
    use, kind = _filter_quotes_for_1d(quotes, "overnight")
    assert "GLW" in use
    assert "ORCL" in use
    assert kind in ("session+rth_holes", "session")


def test_overnight_rejects_unstamped_alt_as_session():
    """无真实印记的备用源不得充当夜盘会话价（曾用墙钟冒充北京15:xx）。"""
    from app.ai_mainline.pipeline import _filter_quotes_for_1d

    quotes = {
        "CRWD": {
            "change_pct": 2.1,
            "quote_source": "alt",
            # 故意无 quote_time：模拟 TradingView/Finviz 补洞
        },
        "GLW": {
            "change_pct": 5.8,
            "quote_source": "cnbc",
            "quote_time_et": "2026-10-06 19:59:00 EDT",
            "quote_time": "2026-10-07 07:59:00",
        },
    }
    use, kind = _filter_quotes_for_1d(quotes, "overnight")
    assert "GLW" in use
    assert "CRWD" not in use
    assert kind == "session"

    only_alt, kind2 = _filter_quotes_for_1d(
        {"CRWD": quotes["CRWD"]}, "overnight"
    )
    assert only_alt == {}
    assert kind2 == "no_ext"


def test_overnight_rejects_pure_rth_close_as_live():
    """夜盘仅有北京04:00/美东16:00时必须 no_ext，禁止 rth_fallback 冒充最新。"""
    from app.ai_mainline.pipeline import _filter_quotes_for_1d, _has_ext_live_1d

    only_rth = {
        "NVDA": {
            "change_pct": -0.5,
            "quote_source": "sina",
            "quote_time_et": "2026-10-09 16:00:00 EDT",
            "quote_time": "2026-10-10 04:00:00",
        }
    }
    use, kind = _filter_quotes_for_1d(only_rth, "overnight")
    assert use == {}
    assert kind == "no_ext"

    assert (
        _has_ext_live_1d(
            {
                "live_1d": True,
                "data_time_1d_bj": "2026-10-10 04:00:00",
                "data_time_1d_et": "2026-10-09 16:00:00 EDT",
                "data_time_1d_source": "live_quote",
            },
            "overnight",
        )
        is False
    )
    assert (
        _has_ext_live_1d(
            {
                "live_1d": True,
                "data_time_1d_bj": "2026-10-10 08:00:00",
                "data_time_1d_et": "2026-10-09 20:00:00 EDT",
                "data_time_1d_source": "live_quote",
            },
            "overnight",
        )
        is True
    )


def test_overlay_refuses_unstamped_alt_as_live_1d(monkeypatch):
    """无真实印记时不得标 live_1d，也不得写入 overlay_refresh 墙钟。"""
    import asyncio

    import app.ai_mainline.pipeline as pipe

    payload = {
        "success": True,
        "enabled": True,
        "trade_date": "2026-10-06",
        "themes": [
            {
                "key": "ai_power",
                "name": "AI电力",
                "members": [{"symbol": "VST", "ret_1d": None}],
            }
        ],
        "bench": {},
        "source": "snapshot",
    }

    async def fake_quotes(phase: str):
        return (
            {"VST": {"change_pct": 1.2, "quote_source": "alt"}},
            "TradingView",
        )

    monkeypatch.setattr(pipe, "_quotes_for_1d_overlay", fake_quotes)
    out, ok = asyncio.run(pipe._overlay_live_1d(payload, phase="overnight"))
    assert ok is False
    assert out.get("live_1d") is False
    assert out.get("1d_pending") is True
    assert out.get("data_time_1d_source") != "overlay_refresh"


def test_settle_and_overnight_lag_tolerates_sparse_ext_quotes():
    """盘后/夜盘印记不能按盘中 2–3 分钟判死，否则会反复 pending 回退 04:00。"""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from app.ai_mainline.pipeline import _1d_lag_too_large

    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    settle_stamp = now - timedelta(minutes=12)
    payload = {
        "live_1d": True,
        "data_time_1d_bj": settle_stamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "data_time_1d_et": settle_stamp.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    assert _1d_lag_too_large(payload, "settle") is False
    assert _1d_lag_too_large(payload, "rth") is True  # 盘中仍严
    # 夜盘：周五盘后戳在周末凌晨仍应可用（<36h）
    fri_post = now - timedelta(hours=10)
    overnight = {
        "live_1d": True,
        "data_time_1d_bj": fri_post.astimezone(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "data_time_1d_et": fri_post.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    assert _1d_lag_too_large(overnight, "overnight") is False


def test_rth_rejects_prior_close_quotes_and_clears_snapshot_1d():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.ai_mainline.pipeline import (
        _clear_intraday_1d_metrics,
        _filter_quotes_for_1d,
        _quote_dt_in_current_session,
    )

    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    prior = {
        "change_pct": 4.2,
        "quote_time_et": "2026-10-02 16:00:00 EDT",
        "quote_time": "2026-10-03 04:00:00",
    }
    assert _quote_dt_in_current_session(prior, "rth", now=now) is False
    live = {
        "change_pct": -1.5,
        "quote_time_et": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "quote_time": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 若当前已是盘中，今日 9:30 后印记应通过
    if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
        assert _quote_dt_in_current_session(live, "rth", now=now) is True

    use, kind = _filter_quotes_for_1d({"MU": prior}, "rth")
    assert use == {}
    assert kind == "no_rth"

    cleared = _clear_intraday_1d_metrics(
        {
            "themes": [
                {
                    "key": "memory",
                    "name": "存储/HBM链",
                    "ret_1d": 3.8,
                    "n_up": 4,
                    "n_valid": 4,
                    "members": [{"symbol": "MU", "ret_1d": 5.0}],
                }
            ],
            "bench": {"ret_1d": 1.2, "ret_5d": 2.0},
        }
    )
    assert cleared["themes"][0]["ret_1d"] is None
    assert cleared["themes"][0]["members"][0]["ret_1d"] is None
    assert cleared["bench"]["ret_1d"] is None
    assert cleared["bench"]["ret_5d"] == 2.0
    assert cleared.get("prior_session_1d", {}).get("memory") == 3.8

    em_live = dict(live)
    em_live["quote_source"] = "eastmoney"
    em_live["change_pct"] = 4.2
    # 有今日盘中印记的东财应可用（Finnhub 限流时的主备源）
    kept_em, kind_em = _filter_quotes_for_1d({"MU": em_live}, "rth")
    if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
        assert kept_em.get("MU") is em_live
        assert kind_em == "rth"
    em_nostamp = {"change_pct": 4.2, "quote_source": "eastmoney"}
    dropped, kind_drop = _filter_quotes_for_1d({"MU": em_nostamp}, "rth")
    assert dropped == {}
    assert kind_drop in ("em_stale", "no_rth")

    fh_live = dict(live)
    fh_live["quote_source"] = "finnhub"
    kept, kind_fh = _filter_quotes_for_1d({"MU": fh_live}, "rth")
    if now.hour > 9 or (now.hour == 9 and now.minute >= 30):
        assert kept.get("MU") is fh_live
        assert kind_fh == "rth"

    from app.ai_mainline.pipeline import _is_prior_session_1d_replay

    replay = _is_prior_session_1d_replay(
        {"memory": 3.8, "power": 2.1, "ai_security": -0.4},
        [
            {"key": "memory", "ret_1d": 3.8},
            {"key": "power", "ret_1d": 2.11},
            {"key": "ai_security", "ret_1d": -0.4},
        ],
    )
    assert replay is True
    moved = _is_prior_session_1d_replay(
        {"memory": 3.8, "power": 2.1, "ai_security": -0.4},
        [
            {"key": "memory", "ret_1d": -1.2},
            {"key": "power", "ret_1d": -0.8},
            {"key": "ai_security", "ret_1d": 2.5},
        ],
    )
    assert moved is False
