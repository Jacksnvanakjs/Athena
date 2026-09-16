"""推送可执行回测：买入=推送后1h价；卖出=T0/T1 开盘与收盘（按时段分流）。

与「发稿日K首日」(first_day_*) 独立：本模块锚点是 pushed_at（北京 naive）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.utils import is_us_trading_day, now_beijing

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")

# 推送时段标签（写入 push_bt_session，供页面说明）
SESSION_PRE = "pre"  # 盘前 00:00–09:29 交易日
SESSION_RTH = "rth"  # 盘中偏早 09:30–14:59
SESSION_RTH_LATE = "rth_late"  # 盘中偏晚 15:00–15:59
SESSION_POST = "post"  # 盘后 ≥16:00 交易日
SESSION_WEEKEND = "weekend"  # 非美股交易日

SESSION_LABEL_ZH = {
    SESSION_PRE: "盘前推送",
    SESSION_RTH: "盘中推送",
    SESSION_RTH_LATE: "盘中偏晚推送",
    SESSION_POST: "盘后推送",
    SESSION_WEEKEND: "周末/假日推送",
}


@dataclass
class PushBacktestOutcome:
    session: str
    buy_price: float | None = None
    buy_at_et: datetime | None = None
    buy_note: str = ""
    t0_date: date | None = None
    t1_date: date | None = None
    ret_t0_open: float | None = None
    ret_t0_close: float | None = None
    ret_t1_open: float | None = None
    ret_t1_close: float | None = None
    px_t0_open: float | None = None
    px_t0_close: float | None = None
    px_t1_open: float | None = None
    px_t1_close: float | None = None
    na_t0_open: str | None = None
    na_t0_close: str | None = None
    na_t1_open: str | None = None
    na_t1_close: str | None = None
    note: str = ""
    pending: bool = False


def _as_bj_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BJ)
    return dt.astimezone(BJ)


def pushed_at_to_et(pushed_at: datetime) -> datetime:
    """pushed_at 为北京 naive → 美东。"""
    return _as_bj_aware(pushed_at).astimezone(ET)


def classify_push_session(push_et: datetime) -> str:
    d = push_et.date()
    if not is_us_trading_day(d):
        return SESSION_WEEKEND
    mins = push_et.hour * 60 + push_et.minute
    if mins < 9 * 60 + 30:
        return SESSION_PRE
    if mins < 15 * 60:
        return SESSION_RTH
    if mins < 16 * 60:
        return SESSION_RTH_LATE
    return SESSION_POST


def next_trading_day_on_or_after(d: date) -> date:
    guard = 0
    while not is_us_trading_day(d) and guard < 20:
        d += timedelta(days=1)
        guard += 1
    return d


def next_trading_day_after(d: date) -> date:
    return next_trading_day_on_or_after(d + timedelta(days=1))


def resolve_t0_t1(buy_et: datetime) -> tuple[date, date, bool, bool, str]:
    """根据买入采样时刻，解析 T0/T1 及 T0 开/收是否可用。

    返回 (t0, t1, allow_t0_open, allow_t0_close, branch_note)
    """
    d = buy_et.date()
    mins = buy_et.hour * 60 + buy_et.minute
    rth_open = 9 * 60 + 30
    rth_close = 16 * 60

    if is_us_trading_day(d) and mins < rth_open:
        t0 = d
        return t0, next_trading_day_after(t0), True, True, "买入在当日开盘前：T0开/收均可用"
    if is_us_trading_day(d) and mins < rth_close:
        t0 = d
        return (
            t0,
            next_trading_day_after(t0),
            False,
            True,
            "买入在当日盘中：T0开盘已过→N/A；T0收盘可用",
        )
    # 已收盘或非交易日 → 下一交易日为 T0
    if is_us_trading_day(d):
        t0 = next_trading_day_after(d)
        return t0, next_trading_day_after(t0), True, True, "买入在当日收盘后：T0=下一交易日"
    t0 = next_trading_day_on_or_after(d)
    return t0, next_trading_day_after(t0), True, True, "买入在非交易日：T0=下一交易日"


def _ret(buy: float, sell: float | None) -> float | None:
    if buy is None or buy <= 0 or sell is None or sell <= 0:
        return None
    return (sell - buy) / buy


async def compute_push_backtest(
    ticker: str,
    pushed_at: datetime,
    *,
    now_et: datetime | None = None,
) -> PushBacktestOutcome:
    """推送可执行四段收益。未到推送+1h 或卖点未发生则 pending / 对应列为 None+na。"""
    from app.market_data.daily_closes import fetch_daily_bars
    from app.market_data.intraday import fetch_price_at

    t = (ticker or "").upper().strip()
    if not t or not pushed_at:
        return PushBacktestOutcome(
            session="",
            note="无推送或无标的",
            na_t0_open="无推送",
            na_t0_close="无推送",
            na_t1_open="无推送",
            na_t1_close="无推送",
        )

    push_et = pushed_at_to_et(pushed_at)
    session = classify_push_session(push_et)
    buy_et = push_et + timedelta(hours=1)
    now_et = now_et or datetime.now(ET)
    out = PushBacktestOutcome(session=session)
    sess_zh = SESSION_LABEL_ZH.get(session, session)

    if buy_et > now_et + timedelta(minutes=2):
        out.pending = True
        out.buy_at_et = buy_et
        out.note = f"{sess_zh}；买入采样点推送+1h尚未到达（{buy_et.strftime('%m-%d %H:%M')}ET）"
        out.na_t0_open = out.na_t0_close = out.na_t1_open = out.na_t1_close = "待买入采样"
        return out

    buy_px, buy_note = await fetch_price_at(t, buy_et)
    out.buy_at_et = buy_et
    out.buy_note = buy_note
    if buy_px is None:
        out.note = f"{sess_zh}；买入价不可用：{buy_note}"
        out.na_t0_open = out.na_t0_close = out.na_t1_open = out.na_t1_close = "无买入价"
        return out
    out.buy_price = buy_px

    t0, t1, allow_open, allow_close, branch = resolve_t0_t1(buy_et)
    out.t0_date = t0
    out.t1_date = t1

    bars = await fetch_daily_bars(t, lookback_days=40)
    by_date = {b[0]: b for b in bars}  # date -> (d,o,h,l,c)

    def _bar(d: date):
        return by_date.get(d)

    # T0 open
    if not allow_open:
        out.na_t0_open = "盘中买入后开盘已过，不可回测「先买再开盘卖」"
    else:
        b0 = _bar(t0)
        if not b0:
            if t0 > now_et.date() or (
                t0 == now_et.date() and (now_et.hour * 60 + now_et.minute) < 9 * 60 + 35
            ):
                out.na_t0_open = "待T0开盘"
                out.pending = True
            else:
                out.na_t0_open = "缺T0日线开盘"
        else:
            out.px_t0_open = float(b0[1])
            out.ret_t0_open = _ret(buy_px, out.px_t0_open)

    # T0 close
    if not allow_close:
        out.na_t0_close = "买入已在收盘后，无当日收盘卖"
    else:
        b0 = _bar(t0)
        if not b0:
            if t0 > now_et.date() or (
                t0 == now_et.date() and (now_et.hour * 60 + now_et.minute) < 16 * 60
            ):
                out.na_t0_close = "待T0收盘"
                out.pending = True
            else:
                out.na_t0_close = "缺T0日线收盘"
        else:
            # 若当天尚未收盘，日线 close 可能是不完整的——用时钟判断
            if t0 == now_et.date() and (now_et.hour * 60 + now_et.minute) < 16 * 60:
                out.na_t0_close = "待T0收盘"
                out.pending = True
            else:
                out.px_t0_close = float(b0[4])
                out.ret_t0_close = _ret(buy_px, out.px_t0_close)

    # T1 open / close
    b1 = _bar(t1)
    if not b1:
        if t1 > now_et.date() or (
            t1 == now_et.date() and (now_et.hour * 60 + now_et.minute) < 9 * 60 + 35
        ):
            out.na_t1_open = "待T1开盘"
            out.na_t1_close = "待T1收盘"
            out.pending = True
        else:
            out.na_t1_open = "缺T1日线开盘"
            out.na_t1_close = "缺T1日线收盘"
    else:
        if t1 == now_et.date() and (now_et.hour * 60 + now_et.minute) < 9 * 60 + 35:
            out.na_t1_open = "待T1开盘"
            out.na_t1_close = "待T1收盘"
            out.pending = True
        else:
            out.px_t1_open = float(b1[1])
            out.ret_t1_open = _ret(buy_px, out.px_t1_open)
            if t1 == now_et.date() and (now_et.hour * 60 + now_et.minute) < 16 * 60:
                out.na_t1_close = "待T1收盘"
                out.pending = True
            else:
                out.px_t1_close = float(b1[4])
                out.ret_t1_close = _ret(buy_px, out.px_t1_close)

    out.note = (
        f"{sess_zh}；买入=推送+1h {buy_et.strftime('%m-%d %H:%M')}ET "
        f"@${buy_px:.2f}（{buy_note}）；T0={t0} T1={t1}；{branch}"
    )
    return out


def apply_push_backtest_to_event(event, outcome: PushBacktestOutcome) -> None:
    event.push_bt_session = (outcome.session or "")[:20]
    event.push_bt_buy_price = outcome.buy_price
    if outcome.buy_at_et is not None:
        # 存北京 naive，与 pushed_at 一致，便于 format_beijing_at_*
        event.push_bt_buy_at = (
            outcome.buy_at_et.astimezone(BJ).replace(tzinfo=None)
        )
    else:
        event.push_bt_buy_at = None
    event.push_bt_t0_date = outcome.t0_date
    event.push_bt_t1_date = outcome.t1_date
    event.push_bt_ret_t0_open = outcome.ret_t0_open
    event.push_bt_ret_t0_close = outcome.ret_t0_close
    event.push_bt_ret_t1_open = outcome.ret_t1_open
    event.push_bt_ret_t1_close = outcome.ret_t1_close
    event.push_bt_px_t0_open = outcome.px_t0_open
    event.push_bt_px_t0_close = outcome.px_t0_close
    event.push_bt_px_t1_open = outcome.px_t1_open
    event.push_bt_px_t1_close = outcome.px_t1_close
    # na 原因压缩进 note 前缀
    na_parts = []
    if outcome.na_t0_open:
        na_parts.append(f"T0开:{outcome.na_t0_open}")
    if outcome.na_t0_close:
        na_parts.append(f"T0收:{outcome.na_t0_close}")
    if outcome.na_t1_open:
        na_parts.append(f"T1开:{outcome.na_t1_open}")
    if outcome.na_t1_close:
        na_parts.append(f"T1收:{outcome.na_t1_close}")
    note = outcome.note or ""
    if na_parts:
        note = f"{note}｜" + "；".join(na_parts)
    event.push_bt_note = note[:500]
    event.push_bt_checked_at = now_beijing()


async def refresh_event_push_backtest(event) -> PushBacktestOutcome | None:
    pushed = getattr(event, "pushed_at", None)
    ticker = getattr(event, "beneficiary_ticker", None) or ""
    if not pushed or not ticker:
        return None
    outcome = await compute_push_backtest(ticker, pushed)
    apply_push_backtest_to_event(event, outcome)
    return outcome


def push_backtest_needs_refresh(event, *, force: bool = False) -> bool:
    if not getattr(event, "pushed_at", None):
        return False
    if force:
        return True
    if getattr(event, "push_bt_checked_at", None) is None:
        return True
    note = getattr(event, "push_bt_note", None) or ""
    if "待" in note or getattr(event, "push_bt_buy_price", None) is None:
        return True
    # 任一关键卖点仍空且 note 含待 → 已在上面；再扫收益是否该有却没有
    return False
