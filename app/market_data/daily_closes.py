"""日线收盘价多源：Yahoo → AKShare(新浪) → Stooq。"""

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


def _normalize(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    cleaned = [(d, float(c)) for d, c in rows if d and c is not None and float(c) > 0]
    cleaned.sort(key=lambda x: x[0])
    # 去重：同日保留后者
    out: dict[date, float] = {}
    for d, c in cleaned:
        out[d] = c
    return sorted(out.items(), key=lambda x: x[0])


async def _from_yahoo(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
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
            closes = (
                (result[0].get("indicators") or {}).get("quote", [{}])[0].get("close")
                or []
            )
            out: list[tuple[date, float]] = []
            for t, cl in zip(ts, closes):
                if cl is None:
                    continue
                d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
                out.append((d, float(cl)))
            if out:
                return _normalize(out)
    return []


async def _from_stooq(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    """Stooq 日线 CSV（美股符号加 .us）。"""
    sym = f"{ticker.lower()}.us"
    url = "https://stooq.com/q/d/l/"
    params = {"s": sym, "i": "d"}
    async with httpx.AsyncClient(
        timeout=25,
        headers={"User-Agent": "Mozilla/5.0"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        text = resp.text.strip()
        if not text or text.lower().startswith("<!"):
            return []
        reader = csv.DictReader(io.StringIO(text))
        out: list[tuple[date, float]] = []
        for row in reader:
            raw_d = (row.get("Date") or row.get("date") or "").strip()
            raw_c = (row.get("Close") or row.get("close") or "").strip()
            if not raw_d or not raw_c:
                continue
            try:
                d = date.fromisoformat(raw_d[:10])
                c = float(raw_c)
            except ValueError:
                continue
            if c > 0:
                out.append((d, c))
        out = _normalize(out)
        if lookback_days and len(out) > lookback_days + 40:
            out = out[-(lookback_days + 40) :]
        return out


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
        # 常见列：date / Date, close / Close
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
        out = _normalize(out)
        if lookback_days and len(out) > lookback_days + 40:
            out = out[-(lookback_days + 40) :]
        return out

    return await asyncio.to_thread(_sync)


async def fetch_daily_closes(
    ticker: str,
    *,
    lookback_days: int = 120,
) -> list[tuple[date, float]]:
    """日线收盘 [(date, close), ...] 升序；多源自动切换。"""
    t = (ticker or "").upper().strip()
    if not t:
        return []

    result = await first_success(
        "daily_closes",
        [
            ("yahoo", lambda: _from_yahoo(t, lookback_days)),
            ("akshare_sina", lambda: _from_akshare_sina(t, lookback_days)),
            ("stooq", lambda: _from_stooq(t, lookback_days)),
        ],
        is_ok=lambda rows: isinstance(rows, list) and len(rows) >= 3,
        context=t,
    )
    if not result:
        return []
    return list(result.value)
