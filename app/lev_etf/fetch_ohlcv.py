"""日频 OHLCV（收盘价×成交量）抓取。"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# (trade_date, close, volume)
OhlcvBar = tuple[date, float, float]

_YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}
_YAHOO_COOL_UNTIL = 0.0


def _parse_yahoo_result(result: dict[str, Any]) -> list[OhlcvBar]:
    ts = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    volumes = q.get("volume") or []
    out: list[OhlcvBar] = []
    for i, t in enumerate(ts):
        cl = closes[i] if i < len(closes) else None
        vol = volumes[i] if i < len(volumes) else None
        if cl is None or vol is None:
            continue
        try:
            c = float(cl)
            v = float(vol)
        except (TypeError, ValueError):
            continue
        if c <= 0 or v < 0:
            continue
        d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
        out.append((d, c, v))
    # 去重保最新
    by: dict[date, OhlcvBar] = {}
    for row in out:
        by[row[0]] = row
    return [by[k] for k in sorted(by)]


async def fetch_ohlcv(
    ticker: str,
    *,
    start: date,
    end: date | None = None,
) -> list[OhlcvBar]:
    """Yahoo chart：period1/period2 拉日线 Close + Volume。"""
    global _YAHOO_COOL_UNTIL
    sym = (ticker or "").upper().strip()
    if not sym:
        return []
    end = end or datetime.now(timezone.utc).date()
    if end < start:
        return []
    loop = asyncio.get_running_loop()
    if _YAHOO_COOL_UNTIL > loop.time():
        return []

    period1 = int(
        datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
    )
    period2 = int(
        datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    )
    params = {"period1": period1, "period2": period2, "interval": "1d"}
    urls = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}",
    )

    def _sync_cffi() -> list[OhlcvBar]:
        try:
            from curl_cffi import requests as creq
        except ImportError:
            return []
        sess = creq.Session(impersonate="chrome")
        for url in urls:
            try:
                resp = sess.get(url, params=params, timeout=30)
            except Exception:
                continue
            if resp.status_code in (429, 403):
                return []
            if resp.status_code != 200:
                continue
            text = resp.text or ""
            if not text.lstrip().startswith("{"):
                continue
            try:
                result = (resp.json().get("chart") or {}).get("result") or []
            except Exception:
                continue
            if not result:
                continue
            bars = _parse_yahoo_result(result[0])
            if bars:
                return bars
        return []

    bars = await asyncio.to_thread(_sync_cffi)
    if bars:
        return bars

    async with httpx.AsyncClient(
        headers=_YAHOO_HEADERS, timeout=30, follow_redirects=True
    ) as client:
        for url in urls:
            try:
                resp = await client.get(url, params=params)
            except Exception:
                continue
            if resp.status_code in (429, 403):
                _YAHOO_COOL_UNTIL = loop.time() + 60.0
                logger.warning("Yahoo %s on lev-etf %s; cool 60s", resp.status_code, sym)
                return []
            if resp.status_code != 200:
                continue
            try:
                result = (resp.json().get("chart") or {}).get("result") or []
            except Exception:
                continue
            if not result:
                continue
            bars = _parse_yahoo_result(result[0])
            if bars:
                return bars
    return []


async def fetch_basket_ohlcv(
    tickers: list[str],
    *,
    start: date,
    end: date | None = None,
    pause_sec: float = 0.35,
) -> dict[str, list[OhlcvBar]]:
    """串行拉篮子，避免 Yahoo 429。"""
    out: dict[str, list[OhlcvBar]] = {}
    for i, sym in enumerate(tickers):
        bars = await fetch_ohlcv(sym, start=start, end=end)
        if bars:
            out[sym] = bars
        else:
            logger.info("lev-etf ohlcv empty: %s", sym)
        if i + 1 < len(tickers) and pause_sec > 0:
            await asyncio.sleep(pause_sec)
    return out
