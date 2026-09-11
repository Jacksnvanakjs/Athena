"""日线：收盘价 / OHLC 多源：东财 → Yahoo → Stooq → AKShare。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from app.market_data.cascade import first_success

logger = logging.getLogger(__name__)

_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 AthenaMarketData/1.0",
    "Referer": "https://finance.yahoo.com/",
}

_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 AthenaMarketData/1.0",
    "Referer": "https://quote.eastmoney.com/",
}
_EM_ULIST_URLS = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
    "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
)
_EM_KLINE_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://82.push2his.eastmoney.com/api/qt/stock/kline/get",
)
_EM_MARKETS = (105, 106, 107)
# symbol → 东财市场码（进程内缓存，避免反复扫 ulist）
_EM_MARKET_CACHE: dict[str, int] = {}
_EM_CACHE_LOADED = False
_EM_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "em_us_market_cache.json"


def _load_em_market_cache() -> None:
    global _EM_CACHE_LOADED
    if _EM_CACHE_LOADED:
        return
    _EM_CACHE_LOADED = True
    try:
        if _EM_CACHE_PATH.is_file():
            raw = json.loads(_EM_CACHE_PATH.read_text(encoding="utf-8"))
            for k, v in (raw or {}).items():
                sym = str(k).upper().strip()
                try:
                    mkt = int(v)
                except (TypeError, ValueError):
                    continue
                if sym and mkt in _EM_MARKETS:
                    _EM_MARKET_CACHE.setdefault(sym, mkt)
    except Exception as exc:
        logger.debug("load em market cache failed: %s", exc)


def _persist_em_market_cache() -> None:
    try:
        _EM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: _EM_MARKET_CACHE[k] for k in sorted(_EM_MARKET_CACHE)}
        _EM_CACHE_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("persist em market cache failed: %s", exc)


def seed_eastmoney_markets(mapping: dict[str, int]) -> None:
    """供报价 ulist 回填市场码，加速后续日 K。"""
    _load_em_market_cache()
    changed = False
    for sym, mkt in mapping.items():
        s = str(sym or "").upper().strip()
        try:
            m = int(mkt)
        except (TypeError, ValueError):
            continue
        if not s or m not in _EM_MARKETS:
            continue
        if _EM_MARKET_CACHE.get(s) != m:
            _EM_MARKET_CACHE[s] = m
            changed = True
    if changed:
        _persist_em_market_cache()


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


async def _em_get_json(
    client: httpx.AsyncClient, urls: tuple[str, ...], params: dict
) -> dict:
    """东财 JSON；先打主域名，失败再短重试/备域名（避免双域名×双次拖垮预算）。"""
    last_exc: Exception | None = None
    for idx, url in enumerate(urls):
        attempts = 2 if idx == 0 else 1
        for attempt in range(attempts):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    return data if isinstance(data, dict) else {}
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.15 * (attempt + 1))
        # 主域名彻底失败再试备域名
    if last_exc:
        logger.debug("eastmoney request failed: %s", last_exc)
    return {}


def _parse_em_klines(klines: list[str] | None, lookback_days: int) -> list[Bar]:
    """东财日 K：``date,open,close,high,low,volume,amount,...``。"""
    out: list[Bar] = []
    for line in klines or []:
        parts = str(line).split(",")
        if len(parts) < 5:
            continue
        try:
            d = date.fromisoformat(parts[0][:10])
            o = float(parts[1])
            c = float(parts[2])
            h = float(parts[3])
            l = float(parts[4])
        except (TypeError, ValueError):
            continue
        if c > 0:
            out.append((d, o, h, l, c))
    out = _normalize_bars(out)
    if lookback_days and len(out) > lookback_days + 40:
        out = out[-(lookback_days + 40) :]
    return out


async def _resolve_eastmoney_markets(
    client: httpx.AsyncClient, symbols: list[str]
) -> dict[str, int]:
    """解析美股 secid 市场码：磁盘/内存缓存优先，缺口再 ulist。"""
    _load_em_market_cache()
    uniq = [s.upper().strip() for s in symbols if s and str(s).strip()]
    out: dict[str, int] = {}
    for sym in uniq:
        cached = _EM_MARKET_CACHE.get(sym)
        if cached is not None:
            out[sym] = cached
    missing = [s for s in uniq if s not in out]
    if not missing:
        return out

    for mkt in _EM_MARKETS:
        still = [s for s in missing if s not in out]
        if not still:
            break
        # 逐只解析，避免多 secid 批量触发东财断连
        for sym in still:
            data = await _em_get_json(
                client,
                _EM_ULIST_URLS,
                {"fltt": "2", "secids": f"{mkt}.{sym}", "fields": "f12,f14,f2"},
            )
            for item in ((data.get("data") or {}).get("diff")) or []:
                got = str(item.get("f12") or "").upper()
                px = item.get("f2")
                if got == sym and px not in (None, "", "-"):
                    out[sym] = mkt
                    _EM_MARKET_CACHE[sym] = mkt
                    break
            await asyncio.sleep(0.05)
    if any(s in out for s in missing):
        _persist_em_market_cache()
    return out


async def _bars_from_eastmoney_secid(
    client: httpx.AsyncClient,
    market: int,
    ticker: str,
    lookback_days: int,
    *,
    try_fallback_markets: bool = False,
) -> list[Bar]:
    lmt = max(lookback_days + 20, 40)
    data = await _em_get_json(
        client,
        _EM_KLINE_URLS,
        {
            "secid": f"{market}.{ticker}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101",
            "fqt": "1",
            "end": "20500101",
            "lmt": str(lmt),
        },
    )
    klines = ((data.get("data") or {}).get("klines")) or []
    bars = _parse_em_klines(klines, lookback_days)
    if bars or not try_fallback_markets:
        return bars
    # 单票路径才扫其它市场，避免批量时 3× 放大请求
    for mkt in _EM_MARKETS:
        if mkt == market:
            continue
        data = await _em_get_json(
            client,
            _EM_KLINE_URLS,
            {
                "secid": f"{mkt}.{ticker}",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": "101",
                "fqt": "1",
                "end": "20500101",
                "lmt": str(lmt),
            },
        )
        klines = ((data.get("data") or {}).get("klines")) or []
        bars = _parse_em_klines(klines, lookback_days)
        if bars:
            _EM_MARKET_CACHE[ticker] = mkt
            _persist_em_market_cache()
            return bars
    return []


async def _bars_from_eastmoney(ticker: str, lookback_days: int) -> list[Bar]:
    t = (ticker or "").upper().strip()
    if not t:
        return []
    async with httpx.AsyncClient(
        headers=_EM_HEADERS,
        timeout=12,
        follow_redirects=True,
        trust_env=False,  # 东财走直连；经海外代理易断连
    ) as client:
        mapping = await _resolve_eastmoney_markets(client, [t])
        mkt = mapping.get(t)
        if mkt is None:
            return []
        return await _bars_from_eastmoney_secid(
            client, mkt, t, lookback_days, try_fallback_markets=True
        )


async def _from_eastmoney(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    bars = await _bars_from_eastmoney(ticker, lookback_days)
    return _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])


async def fetch_eastmoney_closes_many(
    symbols: list[str],
    *,
    lookback_days: int = 60,
    concurrency: int = 3,
) -> dict[str, list[tuple[date, float]]]:
    """批量东财日线：缓存市场码优先直拉 kline，缺口再 ulist（宁缺勿错）。"""
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not uniq:
        return {}
    _load_em_market_cache()
    out: dict[str, list[tuple[date, float]]] = {}
    async with httpx.AsyncClient(
        headers=_EM_HEADERS,
        timeout=8,
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=6, max_keepalive_connections=3),
    ) as client:
        mapping = {s: _EM_MARKET_CACHE[s] for s in uniq if s in _EM_MARKET_CACHE}
        need_resolve = [s for s in uniq if s not in mapping]
        if need_resolve:
            resolved = await _resolve_eastmoney_markets(client, need_resolve)
            mapping.update(resolved)

        sem = asyncio.Semaphore(max(1, min(concurrency, 4)))

        async def one(sym: str) -> None:
            mkt = mapping.get(sym)
            if mkt is None:
                return
            async with sem:
                bars = await _bars_from_eastmoney_secid(
                    client, mkt, sym, lookback_days
                )
                await asyncio.sleep(0.04)
            if len(bars) >= 3:
                out[sym] = _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])

        keys = list(mapping.keys())
        for i in range(0, len(keys), 10):
            await asyncio.gather(*[one(s) for s in keys[i : i + 10]])
            if i + 10 < len(keys):
                await asyncio.sleep(0.12)
    logger.info(
        "eastmoney daily closes %s/%s (mapped %s)",
        len(out),
        len(uniq),
        len(mapping),
    )
    return out


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
    """美股日线多源（轮动起点由 first_success 控制）。

    含：东财、Alpha Vantage、Twelve、Tiingo、Polygon、Marketstack、Yahoo、Stooq、AKShare。
    AllTick 不进轮动，仅作对照检测。无 Key 的源自动跳过。
    """
    from app.market_data.alt_sources import (
        fetch_alpha_vantage_daily_closes,
        fetch_marketstack_daily_closes,
        fetch_polygon_daily_closes,
        fetch_tiingo_daily_closes,
        fetch_twelve_daily_closes,
    )

    t = ticker
    sources: list = [
        ("eastmoney", lambda: _from_eastmoney(t, lookback_days)),
        (
            "alpha_vantage",
            lambda: fetch_alpha_vantage_daily_closes(t, lookback_days=lookback_days),
        ),
        ("twelve_data", lambda: fetch_twelve_daily_closes(t, lookback_days=lookback_days)),
        ("tiingo", lambda: fetch_tiingo_daily_closes(t, lookback_days=lookback_days)),
        ("polygon", lambda: fetch_polygon_daily_closes(t, lookback_days=lookback_days)),
        (
            "marketstack",
            lambda: fetch_marketstack_daily_closes(t, lookback_days=lookback_days),
        ),
    ]
    if not skip_yahoo:
        sources.append(("yahoo", lambda: _from_yahoo(t, lookback_days)))
    sources.extend(
        [
            ("stooq", lambda: _from_stooq(t, lookback_days)),
            ("akshare_sina", lambda: _from_akshare_sina(t, lookback_days)),
        ]
    )
    return sources


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
    """并发拉多标的日线；缺数据的标的不写入（宁缺勿错）。

    先批量东财（快），缺口再逐只走级联。
    """
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not uniq:
        return {}
    if skip_yahoo is None:
        skip_yahoo = _skip_yahoo_daily()

    out: dict[str, list[tuple[date, float]]] = {}
    try:
        em = await fetch_eastmoney_closes_many(
            uniq, lookback_days=lookback_days, concurrency=min(5, concurrency)
        )
        out.update(em)
    except Exception as exc:
        logger.warning("eastmoney batch daily closes failed: %s", exc)

    missing = [s for s in uniq if s not in out]
    if not missing:
        return out

    sem = asyncio.Semaphore(max(1, concurrency))

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

    await asyncio.gather(*[one(s) for s in missing])
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
    sources = [("eastmoney", lambda: _bars_from_eastmoney(t, lookback_days))]
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
