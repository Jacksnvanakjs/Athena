"""日频 OHLCV（收盘价×成交量）抓取。Yahoo 主源，Stooq 回退。"""

from __future__ import annotations

import asyncio
import csv
import io
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
_STOOQ_HEADERS = {"User-Agent": "Mozilla/5.0 AthenaLevEtf/1.0"}


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
    by: dict[date, OhlcvBar] = {}
    for row in out:
        by[row[0]] = row
    return [by[k] for k in sorted(by)]


def _filter_range(bars: list[OhlcvBar], start: date, end: date) -> list[OhlcvBar]:
    return [b for b in bars if start <= b[0] <= end]


def _parse_stooq_csv(text: str) -> list[OhlcvBar]:
    reader = csv.DictReader(io.StringIO(text))
    out: list[OhlcvBar] = []
    for row in reader:
        raw_d = (row.get("Date") or row.get("date") or "").strip()
        raw_c = (row.get("Close") or row.get("close") or "").strip()
        raw_v = (row.get("Volume") or row.get("volume") or "").strip()
        if not raw_d or not raw_c or not raw_v:
            continue
        try:
            d = date.fromisoformat(raw_d[:10])
            c = float(raw_c)
            v = float(raw_v)
        except ValueError:
            continue
        if c <= 0 or v < 0:
            continue
        out.append((d, c, v))
    by: dict[date, OhlcvBar] = {}
    for row in out:
        by[row[0]] = row
    return [by[k] for k in sorted(by)]


async def _fetch_yahoo_ohlcv(
    sym: str,
    *,
    start: date,
    end: date,
) -> list[OhlcvBar]:
    global _YAHOO_COOL_UNTIL
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

    def _sync_cffi() -> list[OhlcvBar] | None:
        """None = rate-limited；[] = 无数据。"""
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
                return None
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

    cffi_bars = await asyncio.to_thread(_sync_cffi)
    if cffi_bars is None:
        _YAHOO_COOL_UNTIL = loop.time() + 60.0
        logger.warning("Yahoo rate-limited on lev-etf %s (cffi); cool 60s", sym)
        return []
    if cffi_bars:
        return cffi_bars

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
                logger.warning(
                    "Yahoo %s on lev-etf %s; cool 60s → Stooq fallback",
                    resp.status_code,
                    sym,
                )
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


async def _fetch_stooq_ohlcv(
    sym: str,
    *,
    start: date,
    end: date,
) -> list[OhlcvBar]:
    """Stooq 日线 CSV（含 Volume）；一次可覆盖多年，适合全量回填。"""
    url = "https://stooq.com/q/d/l/"
    params = {"s": f"{sym.lower()}.us", "i": "d"}
    try:
        async with httpx.AsyncClient(
            timeout=20,
            headers=_STOOQ_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, params=params)
    except Exception as exc:
        logger.info("Stooq lev-etf %s error: %s", sym, exc)
        return []
    if resp.status_code != 200:
        return []
    text = (resp.text or "").strip()
    if (
        not text
        or text.lower().startswith("<!")
        or text.lower().startswith("<html")
        or "text/html" in (resp.headers.get("content-type") or "").lower()
    ):
        return []
    try:
        bars = _parse_stooq_csv(text)
    except Exception:
        return []
    return _filter_range(bars, start, end)


async def _fetch_tiingo_ohlcv(
    sym: str,
    *,
    start: date,
    end: date,
) -> list[OhlcvBar]:
    from app.config import TIINGO_API_KEY

    key = (TIINGO_API_KEY or "").strip()
    if not key:
        return []
    url = f"https://api.tiingo.com/tiingo/daily/{sym}/prices"
    params = {"token": key, "startDate": start.isoformat()}
    try:
        async with httpx.AsyncClient(
            timeout=25,
            headers={
                "User-Agent": "Mozilla/5.0 AthenaLevEtf/1.0",
                "Content-Type": "application/json",
            },
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, params=params)
    except Exception as exc:
        logger.info("Tiingo lev-etf %s error: %s", sym, exc)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[OhlcvBar] = []
    for bar in data:
        try:
            d = date.fromisoformat(str(bar.get("date"))[:10])
            c = float(bar.get("adjClose") or bar.get("close") or 0)
            v = float(bar.get("volume") or 0)
        except (TypeError, ValueError):
            continue
        if d < start or d > end:
            continue
        if c <= 0 or v < 0:
            continue
        out.append((d, c, v))
    by: dict[date, OhlcvBar] = {b[0]: b for b in out}
    return [by[k] for k in sorted(by)]


async def _fetch_polygon_ohlcv(
    sym: str,
    *,
    start: date,
    end: date,
) -> list[OhlcvBar]:
    from app.config import POLYGON_API_KEY

    key = (POLYGON_API_KEY or "").strip()
    if not key:
        return []
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{sym}/range/1/day/"
        f"{start.isoformat()}/{end.isoformat()}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=25,
            headers={"User-Agent": "Mozilla/5.0 AthenaLevEtf/1.0"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(
                url, params={"adjusted": "true", "sort": "asc", "apiKey": key}
            )
    except Exception as exc:
        logger.info("Polygon lev-etf %s error: %s", sym, exc)
        return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except Exception:
        return []
    out: list[OhlcvBar] = []
    for bar in data.get("results") or []:
        ts = bar.get("t")
        try:
            c = float(bar.get("c"))
            v = float(bar.get("v") or 0)
        except (TypeError, ValueError):
            continue
        if not ts or c <= 0 or v < 0:
            continue
        d = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date()
        if d < start or d > end:
            continue
        out.append((d, c, v))
    by: dict[date, OhlcvBar] = {b[0]: b for b in out}
    return [by[k] for k in sorted(by)]


async def fetch_ohlcv(
    ticker: str,
    *,
    start: date,
    end: date | None = None,
) -> list[OhlcvBar]:
    """Yahoo → Stooq → Tiingo → Polygon；遇限流自动换源。"""
    sym = (ticker or "").upper().strip()
    if not sym:
        return []
    end = end or datetime.now(timezone.utc).date()
    if end < start:
        return []

    for name, fn in (
        ("yahoo", _fetch_yahoo_ohlcv),
        ("stooq", _fetch_stooq_ohlcv),
        ("tiingo", _fetch_tiingo_ohlcv),
        ("polygon", _fetch_polygon_ohlcv),
    ):
        bars = await fn(sym, start=start, end=end)
        if bars:
            if name != "yahoo":
                logger.info("lev-etf ohlcv via %s: %s (%s bars)", name, sym, len(bars))
            return bars
    return []


async def fetch_basket_ohlcv(
    tickers: list[str],
    *,
    start: date,
    end: date | None = None,
    pause_sec: float = 0.25,
) -> dict[str, list[OhlcvBar]]:
    """串行拉篮子，避免行情源 429。"""
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
