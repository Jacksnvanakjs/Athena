"""美股是否仍可交易：以「近期有日线收盘」为准（退市/停牌过久 → 不可买）。"""

from __future__ import annotations

from datetime import date, timedelta

from app.market_data.daily_closes import fetch_daily_closes

# 最后一根日线距今超过此天数 → 视为无法在美股正常买入（退市/长期停牌）
DEFAULT_MAX_STALE_DAYS = 14


async def latest_close_date(ticker: str, *, lookback_days: int = 40) -> date | None:
    closes = await fetch_daily_closes(ticker, lookback_days=lookback_days)
    if not closes:
        return None
    return closes[-1][0]


async def is_us_tradable(
    ticker: str,
    *,
    as_of: date | None = None,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
) -> bool:
    """受益方 ticker 是否仍有近期美股日线（可买可回测）。

    无 ticker、无日线、或最后收盘早于 as_of - max_stale_days → False。
    """
    t = (ticker or "").strip().upper()
    if not t:
        return False
    last = await latest_close_date(t)
    if last is None:
        return False
    ref = as_of or date.today()
    return last >= ref - timedelta(days=max_stale_days)
