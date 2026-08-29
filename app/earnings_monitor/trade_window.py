"""POST_ER_BUY_WITHIN_2D：财报后买，约 2 个交易日内卖。

展示原则：每个动作只给一个实操时点；主行北京时间，次行浅色美东。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.earnings_monitor.config import (
    EARNINGS_CHASE_GAP_PCT_BLOCK,
    EARNINGS_HOLD_TRADING_DAYS_MAX,
    EARNINGS_STRATEGY,
)

_ET = ZoneInfo("America/New_York")
_BJ = ZoneInfo("Asia/Shanghai")

SESSION_LABELS = {"AMC": "盘后", "BMO": "盘前", "TBD": "待定"}


def _add_trading_days(d: date, n: int) -> date:
    """用工作日近似交易日（周末顺延）。"""
    cur = d
    added = 0
    step = 1 if n >= 0 else -1
    target = abs(n)
    while added < target:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            added += 1
    return cur


def _et_at(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=_ET)


def _fmt_pair(et_dt: datetime) -> tuple[str, str]:
    """返回 (北京 mm-dd HH:MM, 美东 mm-dd HH:MM)。"""
    bj = et_dt.astimezone(_BJ)
    return bj.strftime("%m-%d %H:%M"), et_dt.strftime("%m-%d %H:%M")


@dataclass
class EarningsTradeWindow:
    strategy: str
    buy_window: str
    sell_window: str
    sell_deadline: str
    buy_window_bj: str
    buy_window_et: str
    sell_window_bj: str
    sell_window_et: str
    sell_deadline_bj: str
    sell_deadline_et: str
    earnings_release_bj: str
    earnings_release_et: str
    buy_window_json: str
    hold_trading_days_max: int


def compute_trade_window(
    earnings_date: date,
    session: str,
) -> EarningsTradeWindow:
    """每个动作只保留一个实操时点。

    AMC：买入=财报日美东 16:05 盘后；卖出首选=次日收盘；最晚=T+2 收盘
    BMO：买入=财报日美东 09:35；卖出首选=当日收盘；最晚=次日收盘
    TBD：按 AMC 保守处理
    """
    sess = (session or "TBD").upper()
    if sess not in ("AMC", "BMO", "TBD"):
        sess = "TBD"
    sess_eff = "AMC" if sess == "TBD" else sess

    t0 = earnings_date
    t1 = _add_trading_days(t0, 1)
    t2 = _add_trading_days(t0, 2)
    hold = EARNINGS_HOLD_TRADING_DAYS_MAX

    if sess_eff == "AMC":
        buy_et = _et_at(t0, 16, 5)
        sell_et = _et_at(t1, 16, 0)
        dead_et = _et_at(t2, 16, 0)
        release_et = buy_et
        plan_buy = ["earnings_day_after_hours_1605"]
        plan_sell = "t_plus_1_close"
        plan_dead = "t_plus_2_close"
    else:
        buy_et = _et_at(t0, 9, 35)
        sell_et = _et_at(t0, 16, 0)
        dead_et = _et_at(t1, 16, 0)
        release_et = buy_et
        plan_buy = ["earnings_day_open_0935"]
        plan_sell = "t0_close"
        plan_dead = "t_plus_1_close"

    buy_bj, buy_et_s = _fmt_pair(buy_et)
    sell_bj, sell_et_s = _fmt_pair(sell_et)
    dead_bj, dead_et_s = _fmt_pair(dead_et)
    rel_bj, rel_et_s = _fmt_pair(release_et)

    note = "（时段待确认）" if sess == "TBD" else ""
    # 入库/推送用的单行：北京主时间 + 备注
    buy = f"{buy_bj} 北京{note}"
    sell = f"{sell_bj} 北京"
    deadline = f"{dead_bj} 北京"

    plan = {
        "strategy": EARNINGS_STRATEGY,
        "session": sess,
        "buy_windows": plan_buy,
        "sell_preferred": plan_sell,
        "sell_deadline": plan_dead,
        "hold_trading_days_max": hold,
        "do_not_buy_before_release": True,
        "chase_gap_pct_block": EARNINGS_CHASE_GAP_PCT_BLOCK,
        "buy_et": buy_et.isoformat(),
        "sell_et": sell_et.isoformat(),
        "deadline_et": dead_et.isoformat(),
    }

    return EarningsTradeWindow(
        strategy=EARNINGS_STRATEGY,
        buy_window=buy,
        sell_window=sell,
        sell_deadline=deadline,
        buy_window_bj=buy_bj,
        buy_window_et=buy_et_s,
        sell_window_bj=sell_bj,
        sell_window_et=sell_et_s,
        sell_deadline_bj=dead_bj,
        sell_deadline_et=dead_et_s,
        earnings_release_bj=rel_bj,
        earnings_release_et=rel_et_s,
        buy_window_json=json.dumps(plan, ensure_ascii=False),
        hold_trading_days_max=hold,
    )


def session_label(session: str) -> str:
    return SESSION_LABELS.get((session or "TBD").upper(), session or "待定")
