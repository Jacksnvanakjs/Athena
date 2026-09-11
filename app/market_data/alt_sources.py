"""备用行情源：TradingView / Finviz / AllTick / Alpha Vantage / Twelve / Tiingo / Polygon / Marketstack。

口径：
- 报价涨跌幅优先用 (price - prev_close) / prev_close；TV 的 change 已是百分比。
- 日线只返回真实收盘序列，缺数不编造。
- 无 API Key 时跳过对应源；TV/Finviz 无需 Key。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    text = str(v).strip().replace(",", "").replace("%", "")
    if not text or text in {"-", "N/A", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _quote_dict(
    symbol: str,
    *,
    price: float,
    change_pct: float,
    volume: float = 0.0,
    name: str | None = None,
) -> dict[str, Any]:
    from app.heatmap import _quote_row

    return _quote_row(
        symbol,
        name=name or symbol,
        price=price,
        change_pct=change_pct,
        volume=volume,
    )


# ── TradingView scanner（无 Key，批量）──────────────────────────────────────


async def fetch_tradingview_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """TradingView america/scan：close + change(%) + volume。"""
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not uniq:
        return {}
    tickers: list[str] = []
    for s in uniq:
        tickers.append(f"NASDAQ:{s}")
        tickers.append(f"NYSE:{s}")
        tickers.append(f"AMEX:{s}")
    out: dict[str, dict[str, Any]] = {}
    async with httpx.AsyncClient(headers=_UA, timeout=20, follow_redirects=True) as client:
        for i in range(0, len(tickers), 90):
            chunk = tickers[i : i + 90]
            payload = {
                "symbols": {"tickers": chunk, "query": {"types": []}},
                "columns": ["close", "change", "change_abs", "volume"],
            }
            try:
                resp = await client.post(
                    "https://scanner.tradingview.com/america/scan",
                    json=payload,
                )
            except Exception as exc:
                logger.warning("TradingView scan failed: %s", exc)
                continue
            if resp.status_code != 200:
                logger.warning("TradingView HTTP %s", resp.status_code)
                continue
            try:
                data = resp.json().get("data") or []
            except Exception:
                continue
            for row in data:
                raw = str(row.get("s") or "")
                sym = raw.split(":")[-1].upper().strip()
                if sym not in uniq or sym in out:
                    continue
                vals = row.get("d") or []
                if len(vals) < 2:
                    continue
                price = _to_float(vals[0])
                chg = _to_float(vals[1])
                vol = _to_float(vals[3]) if len(vals) > 3 else 0.0
                if price is None or price <= 0 or chg is None:
                    continue
                out[sym] = _quote_dict(
                    sym, price=price, change_pct=round(chg, 2), volume=vol or 0.0
                )
            await asyncio.sleep(0.05)
    return out


# ── Finviz（无 Key，逐只）───────────────────────────────────────────────────


def _parse_finviz_snapshot(html: str) -> dict[str, str]:
    cells = [
        re.sub(r"<[^>]+>", "", unescape(x)).strip()
        for x in re.findall(
            r'<td[^>]*class="snapshot-td2[^"]*"[^>]*>(.*?)</td>',
            html,
            flags=re.I | re.S,
        )
    ]
    out: dict[str, str] = {}
    for i in range(0, len(cells) - 1, 2):
        label, value = cells[i], cells[i + 1]
        if label and value:
            out[label] = value
    return out


async def fetch_finviz_quotes(
    symbols: list[str],
    *,
    concurrency: int = 4,
    limit: int | None = 20,
) -> dict[str, dict[str, Any]]:
    """Finviz quote 页：Price / Prev Close → 涨跌幅。"""
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if limit is not None:
        uniq = uniq[: max(0, limit)]
    if not uniq:
        return {}
    out: dict[str, dict[str, Any]] = {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(client: httpx.AsyncClient, sym: str) -> None:
        async with sem:
            try:
                resp = await client.get(
                    "https://finviz.com/quote.ashx", params={"t": sym}
                )
            except Exception as exc:
                logger.debug("Finviz %s failed: %s", sym, exc)
                return
            if resp.status_code != 200:
                return
            snap = _parse_finviz_snapshot(resp.text)
            price = _to_float(snap.get("Price"))
            prev = _to_float(snap.get("Prev Close"))
            vol = _to_float(snap.get("Volume")) or 0.0
            if price is None or price <= 0:
                return
            if prev and prev > 0:
                chg = round((price - prev) / prev * 100, 2)
            else:
                chg = 0.0
            out[sym] = _quote_dict(sym, price=price, change_pct=chg, volume=vol)

    async with httpx.AsyncClient(headers=_UA, timeout=20, follow_redirects=True) as client:
        await asyncio.gather(*[one(client, s) for s in uniq])
    return out


# ── AllTick ─────────────────────────────────────────────────────────────────


def _alltick_token() -> str:
    from app.config import ALLTICK_TOKEN

    return (ALLTICK_TOKEN or "").strip()


async def fetch_alltick_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """AllTick /trade-tick：最新成交价；涨跌用近 2 根日 K 估算。"""
    token = _alltick_token()
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not token or not uniq:
        return {}
    out: dict[str, dict[str, Any]] = {}
    query = {
        "trace": "athena-trade-tick",
        "data": {"symbol_list": [{"code": f"{s}.US"} for s in uniq]},
    }
    url = (
        "https://quote.alltick.co/quote-stock-b-api/trade-tick"
        f"?token={quote(token)}&query={quote(json.dumps(query, separators=(',', ':')))}"
    )
    async with httpx.AsyncClient(headers=_UA, timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            body = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.warning("AllTick trade-tick failed: %s", exc)
            return {}
        ticks = ((body.get("data") or {}).get("tick_list")) or []
        prices: dict[str, float] = {}
        volumes: dict[str, float] = {}
        for item in ticks:
            code = str(item.get("code") or "")
            sym = code.replace(".US", "").upper().strip()
            px = _to_float(item.get("price"))
            if sym and px and px > 0:
                prices[sym] = px
                volumes[sym] = _to_float(item.get("volume")) or 0.0

        sem = asyncio.Semaphore(3)

        async def prev_close(sym: str) -> tuple[str, float | None]:
            q = {
                "trace": f"athena-k-{sym}",
                "data": {
                    "code": f"{sym}.US",
                    "kline_type": 8,
                    "kline_timestamp_end": 0,
                    "query_kline_num": 2,
                    "adjust_type": 0,
                },
            }
            u = (
                "https://quote.alltick.co/quote-stock-b-api/kline"
                f"?token={quote(token)}&query={quote(json.dumps(q, separators=(',', ':')))}"
            )
            async with sem:
                try:
                    r = await client.get(u)
                    data = r.json() if r.status_code == 200 else {}
                except Exception:
                    return sym, None
            klines = ((data.get("data") or {}).get("kline_list")) or []
            if len(klines) < 2:
                return sym, None
            ordered = sorted(klines, key=lambda x: int(x.get("timestamp") or 0))
            return sym, _to_float(ordered[0].get("close_price"))

        need = [s for s in prices if s in uniq]
        prevs = dict(await asyncio.gather(*[prev_close(s) for s in need]))
        for sym, px in prices.items():
            if sym not in uniq:
                continue
            prev = prevs.get(sym)
            if prev and prev > 0:
                chg = round((px - prev) / prev * 100, 2)
            else:
                chg = 0.0
            out[sym] = _quote_dict(
                sym, price=px, change_pct=chg, volume=volumes.get(sym) or 0.0
            )
    return out


async def fetch_alltick_daily_closes(
    ticker: str, *, lookback_days: int = 60
) -> list[tuple[date, float]]:
    token = _alltick_token()
    t = (ticker or "").upper().strip()
    if not token or not t:
        return []
    q = {
        "trace": f"athena-daily-{t}",
        "data": {
            "code": f"{t}.US",
            "kline_type": 8,
            "kline_timestamp_end": 0,
            "query_kline_num": max(lookback_days + 10, 40),
            "adjust_type": 0,
        },
    }
    url = (
        "https://quote.alltick.co/quote-stock-b-api/kline"
        f"?token={quote(token)}&query={quote(json.dumps(q, separators=(',', ':')))}"
    )
    async with httpx.AsyncClient(headers=_UA, timeout=20, follow_redirects=True) as client:
        try:
            resp = await client.get(url)
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.debug("AllTick daily %s: %s", t, exc)
            return []
    klines = ((data.get("data") or {}).get("kline_list")) or []
    rows: list[tuple[date, float]] = []
    for item in klines:
        ts = item.get("timestamp")
        close = _to_float(item.get("close_price"))
        if not ts or close is None or close <= 0:
            continue
        try:
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
        except (TypeError, ValueError, OSError):
            continue
        rows.append((d, close))
    rows.sort(key=lambda x: x[0])
    dedup: dict[date, float] = {d: c for d, c in rows}
    return sorted(dedup.items(), key=lambda x: x[0])


# ── Alpha Vantage ───────────────────────────────────────────────────────────


def _av_key() -> str:
    from app.config import ALPHA_VANTAGE_API_KEY

    return (ALPHA_VANTAGE_API_KEY or "").strip()


async def fetch_alpha_vantage_quote(symbol: str) -> dict[str, dict[str, Any]]:
    key = _av_key()
    sym = (symbol or "").upper().strip()
    if not key or not sym:
        return {}
    params = {"function": "GLOBAL_QUOTE", "symbol": sym, "apikey": key}
    async with httpx.AsyncClient(headers=_UA, timeout=20) as client:
        try:
            resp = await client.get("https://www.alphavantage.co/query", params=params)
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.debug("AlphaVantage quote %s: %s", sym, exc)
            return {}
    if data.get("Note") or data.get("Information"):
        logger.warning("Alpha Vantage rate limited")
        return {}
    q = data.get("Global Quote") or {}
    price = _to_float(q.get("05. price"))
    prev = _to_float(q.get("08. previous close"))
    chg = _to_float(str(q.get("10. change percent") or "").replace("%", ""))
    vol = _to_float(q.get("06. volume")) or 0.0
    if price is None or price <= 0:
        return {}
    if (chg is None) and prev and prev > 0:
        chg = round((price - prev) / prev * 100, 2)
    return {sym: _quote_dict(sym, price=price, change_pct=float(chg or 0.0), volume=vol)}


async def fetch_alpha_vantage_daily_closes(
    ticker: str, *, lookback_days: int = 60
) -> list[tuple[date, float]]:
    key = _av_key()
    t = (ticker or "").upper().strip()
    if not key or not t:
        return []
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": t,
        "outputsize": "compact",
        "apikey": key,
    }
    async with httpx.AsyncClient(headers=_UA, timeout=25) as client:
        try:
            resp = await client.get("https://www.alphavantage.co/query", params=params)
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.debug("AlphaVantage daily %s: %s", t, exc)
            return []
    if data.get("Note") or data.get("Information"):
        logger.warning("Alpha Vantage rate limited (daily)")
        return []
    series = data.get("Time Series (Daily)") or {}
    rows: list[tuple[date, float]] = []
    for ds, bar in series.items():
        try:
            d = date.fromisoformat(str(ds)[:10])
            c = float(bar.get("4. close"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            rows.append((d, c))
    rows.sort(key=lambda x: x[0])
    if lookback_days and len(rows) > lookback_days + 40:
        rows = rows[-(lookback_days + 40) :]
    return rows


async def fetch_twelve_daily_closes(
    ticker: str, *, lookback_days: int = 60
) -> list[tuple[date, float]]:
    from app.config import TWELVE_DATA_API_KEY

    key = (TWELVE_DATA_API_KEY or "").strip()
    t = (ticker or "").upper().strip()
    if not key or not t:
        return []
    params = {
        "symbol": t,
        "interval": "1day",
        "outputsize": max(lookback_days + 10, 40),
        "apikey": key,
    }
    async with httpx.AsyncClient(headers=_UA, timeout=20) as client:
        try:
            resp = await client.get("https://api.twelvedata.com/time_series", params=params)
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.debug("TwelveData %s: %s", t, exc)
            return []
    if data.get("status") == "error":
        return []
    rows: list[tuple[date, float]] = []
    for bar in data.get("values") or []:
        try:
            d = date.fromisoformat(str(bar.get("datetime"))[:10])
            c = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            rows.append((d, c))
    rows.sort(key=lambda x: x[0])
    return rows


async def fetch_tiingo_daily_closes(
    ticker: str, *, lookback_days: int = 60
) -> list[tuple[date, float]]:
    from app.config import TIINGO_API_KEY

    key = (TIINGO_API_KEY or "").strip()
    t = (ticker or "").upper().strip()
    if not key or not t:
        return []
    url = f"https://api.tiingo.com/tiingo/daily/{t}/prices"
    start = (date.today() - timedelta(days=max(lookback_days + 40, 120))).isoformat()
    params = {"token": key, "startDate": start}
    async with httpx.AsyncClient(
        headers={**_UA, "Content-Type": "application/json"}, timeout=20
    ) as client:
        try:
            resp = await client.get(url, params=params)
            data = resp.json() if resp.status_code == 200 else []
        except Exception as exc:
            logger.debug("Tiingo %s: %s", t, exc)
            return []
    if not isinstance(data, list):
        return []
    rows: list[tuple[date, float]] = []
    for bar in data:
        try:
            d = date.fromisoformat(str(bar.get("date"))[:10])
            c = float(bar.get("adjClose") or bar.get("close"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            rows.append((d, c))
    rows.sort(key=lambda x: x[0])
    if lookback_days and len(rows) > lookback_days + 40:
        rows = rows[-(lookback_days + 40) :]
    return rows


async def fetch_polygon_daily_closes(
    ticker: str, *, lookback_days: int = 60
) -> list[tuple[date, float]]:
    from app.config import POLYGON_API_KEY

    key = (POLYGON_API_KEY or "").strip()
    t = (ticker or "").upper().strip()
    if not key or not t:
        return []
    end = date.today()
    start = end - timedelta(days=max(lookback_days + 40, 90))
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{t}/range/1/day/"
        f"{start.isoformat()}/{end.isoformat()}"
    )
    async with httpx.AsyncClient(headers=_UA, timeout=20) as client:
        try:
            resp = await client.get(
                url, params={"adjusted": "true", "sort": "asc", "apiKey": key}
            )
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.debug("Polygon %s: %s", t, exc)
            return []
    rows: list[tuple[date, float]] = []
    for bar in data.get("results") or []:
        ts = bar.get("t")
        c = _to_float(bar.get("c"))
        if not ts or c is None or c <= 0:
            continue
        d = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).date()
        rows.append((d, c))
    rows.sort(key=lambda x: x[0])
    return rows


async def fetch_marketstack_daily_closes(
    ticker: str, *, lookback_days: int = 60
) -> list[tuple[date, float]]:
    from app.config import MARKETSTACK_API_KEY

    key = (MARKETSTACK_API_KEY or "").strip()
    t = (ticker or "").upper().strip()
    if not key or not t:
        return []
    params = {
        "access_key": key,
        "symbols": t,
        "limit": max(lookback_days + 10, 40),
    }
    async with httpx.AsyncClient(headers=_UA, timeout=20) as client:
        try:
            resp = await client.get("http://api.marketstack.com/v1/eod", params=params)
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            logger.debug("Marketstack %s: %s", t, exc)
            return []
    rows: list[tuple[date, float]] = []
    for bar in data.get("data") or []:
        try:
            d = date.fromisoformat(str(bar.get("date"))[:10])
            c = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            rows.append((d, c))
    rows.sort(key=lambda x: x[0])
    return rows


async def fill_quotes_rotating(
    symbols: list[str],
    *,
    existing: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """对缺票按轮动顺序补报价；返回 (merged_additions, used_source_labels)。"""
    from app.config import ALPHA_VANTAGE_API_KEY
    from app.market_data.cascade import next_batch_order

    missing = [
        s.upper().strip()
        for s in symbols
        if s and str(s).strip() and s.upper().strip() not in (existing or {})
    ]
    if not missing:
        return {}, []

    candidates: list[tuple[str, Any]] = [
        ("TradingView", lambda syms: fetch_tradingview_quotes(syms)),
        ("Finviz", lambda syms: fetch_finviz_quotes(syms, limit=min(15, len(syms)))),
    ]
    # AllTick 仅作对照检测，不进轮动（见 /api/ai-mainline/verify-alltick）
    if (ALPHA_VANTAGE_API_KEY or "").strip():

        async def _av(syms: list[str]) -> dict[str, dict[str, Any]]:
            out: dict[str, dict[str, Any]] = {}
            for s in syms[:2]:
                part = await fetch_alpha_vantage_quote(s)
                out.update(part)
                await asyncio.sleep(12.5)
            return out

        candidates.append(("AlphaVantage", _av))

    order = next_batch_order("quote_fill", [n for n, _ in candidates])
    by_name = dict(candidates)
    added: dict[str, dict[str, Any]] = {}
    used: list[str] = []
    still = list(missing)
    for name in order:
        if not still:
            break
        factory = by_name[name]
        try:
            part = await factory(still)
        except Exception as exc:
            logger.warning("quote fill %s failed: %s", name, exc)
            continue
        if not part:
            continue
        added.update(part)
        used.append(f"{name}:{len(part)}")
        still = [s for s in still if s not in added]
        logger.info("quote fill hit %s (+%s, still %s)", name, len(part), len(still))
    return added, used
