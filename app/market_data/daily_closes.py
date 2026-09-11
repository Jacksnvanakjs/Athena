"""日线：收盘价 / OHLC 多源：Yahoo → AKShare(新浪) → Stooq。"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, datetime, timezone

import httpx

from app.market_data.cascade import first_success

logger = logging.getLogger(__name__)

_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 AthenaMarketData/1.0",
    "Referer": "https://finance.yahoo.com/",
}

# (date, open, high, low, close)
Bar = tuple[date, float, float, float, float]


def _normalize_closes(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    cleaned = [(d, float(c)) for d, c in rows if d and c is not None and float(c) > 0]
    cleaned.sort(key=lambda x: x[0])
    out: dict[date, float] = {}
    for d, c in cleaned:
        out[d] = c
    return sorted(out.items(), key=lambda x: x[0])


def _normalize_bars(rows: list[Bar]) -> list[Bar]:
    cleaned: list[Bar] = []
    for row in rows:
        if not row or row[0] is None:
            continue
        d, o, h, l, c = row
        try:
            o, h, l, c = float(o), float(h), float(l), float(c)
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        if o <= 0:
            o = c
        if h <= 0:
            h = max(o, c)
        if l <= 0:
            l = min(o, c)
        cleaned.append((d, o, h, l, c))
    cleaned.sort(key=lambda x: x[0])
    out: dict[date, Bar] = {}
    for row in cleaned:
        out[row[0]] = row
    return [out[k] for k in sorted(out)]


async def _bars_from_yahoo(ticker: str, lookback_days: int) -> list[Bar]:
    span = max(lookback_days + 40, 60)
    urls = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
    )
    params = {"range": f"{span}d", "interval": "1d"}
    async with httpx.AsyncClient(
        headers=_YAHOO_HEADERS, timeout=25, follow_redirects=True
    ) as client:
        for url in urls:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                await asyncio.sleep(1.5)
                resp = await client.get(url, params=params)
            if resp.status_code != 200:
                continue
            result = (resp.json().get("chart") or {}).get("result") or []
            if not result:
                continue
            ts = result[0].get("timestamp") or []
            q = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            opens = q.get("open") or []
            highs = q.get("high") or []
            lows = q.get("low") or []
            closes = q.get("close") or []
            out: list[Bar] = []
            for i, t in enumerate(ts):
                cl = closes[i] if i < len(closes) else None
                if cl is None:
                    continue
                d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
                o = opens[i] if i < len(opens) and opens[i] is not None else cl
                h = highs[i] if i < len(highs) and highs[i] is not None else cl
                l = lows[i] if i < len(lows) and lows[i] is not None else cl
                out.append((d, float(o), float(h), float(l), float(cl)))
            if out:
                return _normalize_bars(out)
    return []


async def _from_yahoo(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    bars = await _bars_from_yahoo(ticker, lookback_days)
    return _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])


async def _bars_from_stooq(ticker: str, lookback_days: int) -> list[Bar]:
    sym = f"{ticker.lower()}.us"
    url = "https://stooq.com/q/d/l/"
    params = {"s": sym, "i": "d"}
    async with httpx.AsyncClient(
        timeout=8,
        headers={"User-Agent": "Mozilla/5.0 AthenaMarketData/1.0"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        text = resp.text.strip()
        # 反爬/挑战页常为短 HTML，勿当 CSV 解析
        if (
            not text
            or text.lower().startswith("<!")
            or text.lower().startswith("<html")
            or "text/html" in (resp.headers.get("content-type") or "").lower()
        ):
            return []
        reader = csv.DictReader(io.StringIO(text))
        out: list[Bar] = []
        for row in reader:
            raw_d = (row.get("Date") or row.get("date") or "").strip()
            raw_o = (row.get("Open") or row.get("open") or "").strip()
            raw_h = (row.get("High") or row.get("high") or "").strip()
            raw_l = (row.get("Low") or row.get("low") or "").strip()
            raw_c = (row.get("Close") or row.get("close") or "").strip()
            if not raw_d or not raw_c:
                continue
            try:
                d = date.fromisoformat(raw_d[:10])
                c = float(raw_c)
                o = float(raw_o) if raw_o else c
                h = float(raw_h) if raw_h else c
                l = float(raw_l) if raw_l else c
            except ValueError:
                continue
            if c > 0:
                out.append((d, o, h, l, c))
        out = _normalize_bars(out)
        if lookback_days and len(out) > lookback_days + 40:
            out = out[-(lookback_days + 40) :]
        return out


async def _from_stooq(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    bars = await _bars_from_stooq(ticker, lookback_days)
    return _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])


async def _from_akshare_sina(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    def _sync() -> list[tuple[date, float]]:
        try:
            import akshare as ak
        except ImportError:
            return []
        try:
            df = ak.stock_us_daily(symbol=ticker.upper(), adjust="")
        except Exception:
            try:
                df = ak.stock_us_daily(symbol=ticker.upper())
            except Exception as exc:
                logger.debug("akshare daily %s: %s", ticker, exc)
                return []
        if df is None or getattr(df, "empty", True):
            return []
        cols = {str(c).lower(): c for c in df.columns}
        dcol = cols.get("date") or cols.get("日期")
        ccol = cols.get("close") or cols.get("收盘")
        if dcol is None or ccol is None:
            return []
        out: list[tuple[date, float]] = []
        for _, row in df.iterrows():
            raw_d = row[dcol]
            raw_c = row[ccol]
            try:
                if hasattr(raw_d, "date"):
                    d = raw_d.date()
                else:
                    d = date.fromisoformat(str(raw_d)[:10])
                c = float(raw_c)
            except Exception:
                continue
            if c > 0:
                out.append((d, c))
        out = _normalize_closes(out)
        if lookback_days and len(out) > lookback_days + 40:
            out = out[-(lookback_days + 40) :]
        return out

    return await asyncio.to_thread(_sync)


def _skip_yahoo_daily() -> bool:
    import os

    return os.environ.get("HEATMAP_SKIP_YAHOO", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _daily_close_sources(ticker: str, lookback_days: int, *, skip_yahoo: bool):
    """可靠美股日线顺序。

    跳过 Yahoo 时优先 Stooq（美股原生 CSV），再 AKShare；避免先卡在慢/不稳的新浪全表。
    """
    t = ticker
    if skip_yahoo:
        return [
            ("stooq", lambda: _from_stooq(t, lookback_days)),
            ("akshare_sina", lambda: _from_akshare_sina(t, lookback_days)),
        ]
    return [
        ("yahoo", lambda: _from_yahoo(t, lookback_days)),
        ("stooq", lambda: _from_stooq(t, lookback_days)),
        ("akshare_sina", lambda: _from_akshare_sina(t, lookback_days)),
    ]


async def fetch_daily_closes(
    ticker: str,
    *,
    lookback_days: int = 120,
    skip_yahoo: bool | None = None,
) -> list[tuple[date, float]]:
    """日线收盘 [(date, close), ...] 升序；多源自动切换。"""
    t = (ticker or "").upper().strip()
    if not t:
        return []
    if skip_yahoo is None:
        skip_yahoo = _skip_yahoo_daily()

    result = await first_success(
        "daily_closes",
        _daily_close_sources(t, lookback_days, skip_yahoo=skip_yahoo),
        is_ok=lambda rows: isinstance(rows, list) and len(rows) >= 3,
        context=t,
    )
    if not result:
        return []
    return list(result.value)


async def fetch_daily_closes_many(
    symbols: list[str],
    *,
    lookback_days: int = 60,
    concurrency: int = 8,
    skip_yahoo: bool | None = None,
) -> dict[str, list[tuple[date, float]]]:
    """并发拉多标的日线；缺数据的标的不写入（宁缺勿错）。"""
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not uniq:
        return {}
    if skip_yahoo is None:
        skip_yahoo = _skip_yahoo_daily()
    sem = asyncio.Semaphore(max(1, concurrency))
    out: dict[str, list[tuple[date, float]]] = {}

    async def one(sym: str) -> None:
        async with sem:
            try:
                rows = await fetch_daily_closes(
                    sym, lookback_days=lookback_days, skip_yahoo=skip_yahoo
                )
            except Exception as exc:
                logger.debug("daily closes %s failed: %s", sym, exc)
                return
            if rows:
                out[sym] = rows

    await asyncio.gather(*[one(s) for s in uniq])
    return out


async def fetch_daily_bars(
    ticker: str,
    *,
    lookback_days: int = 120,
    skip_yahoo: bool | None = None,
) -> list[Bar]:
    """日线 OHLC [(date, open, high, low, close), ...] 升序。"""
    t = (ticker or "").upper().strip()
    if not t:
        return []
    if skip_yahoo is None:
        skip_yahoo = _skip_yahoo_daily()
    sources = []
    if not skip_yahoo:
        sources.append(("yahoo", lambda: _bars_from_yahoo(t, lookback_days)))
    sources.append(("stooq", lambda: _bars_from_stooq(t, lookback_days)))

    result = await first_success(
        "daily_bars",
        sources,
        is_ok=lambda rows: isinstance(rows, list) and len(rows) >= 3,
        context=t,
    )
    if not result:
        closes = await fetch_daily_closes(t, lookback_days=lookback_days)
        return [(d, c, c, c, c) for d, c in closes]
    return list(result.value)
