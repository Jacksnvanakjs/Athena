"""财报日历「距今」北京时间标签。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.earnings_monitor.pipeline import days_to_earnings
from app.earnings_monitor.trade_window import relative_release_label_bj

_BJ = ZoneInfo("Asia/Shanghai")


def test_amc_earnings_tomorrow_early_morning_bj():
    # 美东 9/3 盘后 → 北京 9/4 凌晨
    now = datetime(2026, 9, 3, 20, 0, tzinfo=_BJ)
    label = relative_release_label_bj(date(2026, 9, 3), "AMC", now=now)
    assert label == "明日凌晨"
    assert days_to_earnings(date(2026, 9, 3), today=now.date(), session="AMC") == 1


def test_bmo_earnings_tonight_bj():
    # 美东 9/3 盘前 09:35 → 北京 9/3 21:35
    now = datetime(2026, 9, 3, 18, 0, tzinfo=_BJ)
    label = relative_release_label_bj(date(2026, 9, 3), "BMO", now=now)
    assert label == "今晚"


def test_bmo_earnings_tomorrow_morning_bj():
    now = datetime(2026, 9, 2, 22, 0, tzinfo=_BJ)
    label = relative_release_label_bj(date(2026, 9, 3), "BMO", now=now)
    assert label == "明日"
