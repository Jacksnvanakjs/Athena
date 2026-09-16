"""历史分钟/扩展时段价：用于推送后 1h 买入回测。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


async def fetch_price_at(
    ticker: str,
    when_et: datetime,
    *,
    tolerance_minutes: int = 20,
) -> tuple[float | None, str]:
    """取 when_et（美东）附近成交价；优先 ≤目标的最近一根 5 分钟 K（含盘前盘后）。

    返回 (price, note)。无数据则 (None, reason)。
    """
    t = (ticker or "").upper().strip()
    if not t or when_et is None:
        return None, "缺标的或时间"
    if when_et.tzinfo is None:
        when_et = when_et.replace(tzinfo=ET)
    else:
        when_et = when_et.astimezone(ET)

    # Yahoo 5m 窗口：目标前后各约 2 天，覆盖隔夜
    start = when_et - timedelta(days=2)
    end = when_et + timedelta(days=1)
    period1 = int(start.timestamp())
    period2 = int(end.timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}"
    params = {
        "interval": "5m",
        "period1": period1,
        "period2": period2,
        "includePrePost": "true",
    }
    headers = {"User-Agent": "Mozilla/5.0 AthenaPushBacktest/1.0"}
    try:
        async with httpx.AsyncClient(timeout=25.0, headers=headers) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        logger.info("intraday price %s @ %s failed: %s", t, when_et, exc)
        return None, f"分钟线拉取失败:{type(exc).__name__}"

    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return None, "无分钟线"
    ts = result.get("timestamp") or []
    quote = (result.get("indicators") or {}).get("quote") or [{}]
    closes = (quote[0] or {}).get("close") or []
    opens = (quote[0] or {}).get("open") or []

    best_before: tuple[float, datetime, float] | None = None  # |delta|, dt, px
    best_after: tuple[float, datetime, float] | None = None
    for i, raw_ts in enumerate(ts):
        cl = closes[i] if i < len(closes) else None
        op = opens[i] if i < len(opens) else None
        px = cl if cl is not None else op
        if px is None:
            continue
        try:
            px_f = float(px)
        except (TypeError, ValueError):
            continue
        if px_f <= 0:
            continue
        bar_et = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc).astimezone(ET)
        delta = (bar_et - when_et).total_seconds() / 60.0
        ad = abs(delta)
        if delta <= 0:
            if best_before is None or ad < best_before[0]:
                best_before = (ad, bar_et, px_f)
        else:
            if best_after is None or ad < best_after[0]:
                best_after = (ad, bar_et, px_f)

    tol = max(5, int(tolerance_minutes))
    chosen = None
    if best_before and best_before[0] <= tol:
        chosen = best_before
    elif best_after and best_after[0] <= tol:
        chosen = best_after
    elif best_before:
        chosen = best_before
    elif best_after:
        chosen = best_after

    if not chosen:
        return None, "目标时刻无分钟K"
    ad, bar_et, px_f = chosen
    if ad > tol:
        return (
            round(px_f, 4),
            f"最近K偏离{ad:.0f}分@{bar_et.strftime('%m-%d %H:%M')}ET",
        )
    return (
        round(px_f, 4),
        f"5m@{bar_et.strftime('%Y-%m-%d %H:%M')}ET",
    )
