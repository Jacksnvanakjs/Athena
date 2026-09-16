"""推送回测时段分流（无外网）。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.deal_monitor.push_backtest import (
    SESSION_POST,
    SESSION_PRE,
    SESSION_RTH,
    SESSION_RTH_LATE,
    SESSION_WEEKEND,
    classify_push_session,
    resolve_t0_t1,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_classify_sessions():
    # 2026-09-14 Monday
    assert classify_push_session(_et(2026, 9, 14, 8, 0)) == SESSION_PRE
    assert classify_push_session(_et(2026, 9, 14, 10, 45)) == SESSION_RTH
    assert classify_push_session(_et(2026, 9, 14, 15, 30)) == SESSION_RTH_LATE
    assert classify_push_session(_et(2026, 9, 14, 20, 0)) == SESSION_POST
    assert classify_push_session(_et(2026, 9, 13, 12, 0)) == SESSION_WEEKEND  # Sunday


def test_resolve_rth_buy_na_open():
    buy = _et(2026, 9, 14, 11, 45)  # push 10:45 +1h
    t0, t1, allow_o, allow_c, _ = resolve_t0_t1(buy)
    assert t0.isoformat() == "2026-09-14"
    assert t1.isoformat() == "2026-09-15"
    assert allow_o is False
    assert allow_c is True


def test_resolve_post_buy_next_day():
    buy = _et(2026, 9, 14, 21, 0)
    t0, t1, allow_o, allow_c, _ = resolve_t0_t1(buy)
    assert t0.isoformat() == "2026-09-15"
    assert t1.isoformat() == "2026-09-16"
    assert allow_o is True
    assert allow_c is True


def test_resolve_pre_buy_same_day_open_ok():
    buy = _et(2026, 9, 14, 9, 0)
    t0, t1, allow_o, allow_c, _ = resolve_t0_t1(buy)
    assert t0.isoformat() == "2026-09-14"
    assert allow_o is True
    assert allow_c is True
