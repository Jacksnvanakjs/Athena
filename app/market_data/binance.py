"""币安公开行情（免 API Key）：报价 + 日 K。

文档：https://developers.binance.com/en/docs/introduction
市场数据专用域名：https://data-api.binance.vision （见 market_data_only FAQ）

仅覆盖可映射到现货交易对的加密货币符号（如 BTC→BTCUSDT）；
美股 ticker（NVDA/COIN 等）无对应现货对时直接跳过，不进 HTTP。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": "AthenaMarketData/1.0 (+https://github.com/Jacksnvanakjs/Athena)",
}

# 常用美股/Yahoo 风格别名 → 币安现货 USDT 对
_SYMBOL_ALIAS: dict[str, str] = {
    "BTC": "BTCUSDT",
    "BTCUSD": "BTCUSDT",
    "BTC-USD": "BTCUSDT",
    "XBT": "BTCUSDT",
    "XBTUSD": "BTCUSDT",
    "ETH": "ETHUSDT",
    "ETHUSD": "ETHUSDT",
    "ETH-USD": "ETHUSDT",
    "SOL": "SOLUSDT",
    "SOLUSD": "SOLUSDT",
    "SOL-USD": "SOLUSDT",
    "BNB": "BNBUSDT",
    "BNBUSD": "BNBUSDT",
    "BNB-USD": "BNBUSDT",
    "XRP": "XRPUSDT",
    "XRPUSD": "XRPUSDT",
    "XRP-USD": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "DOGEUSD": "DOGEUSDT",
    "DOGE-USD": "DOGEUSDT",
    "ADA": "ADAUSDT",
    "ADAUSD": "ADAUSDT",
    "ADA-USD": "ADAUSDT",
    "AVAX": "AVAXUSDT",
    "AVAXUSD": "AVAXUSDT",
    "AVAX-USD": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "LINKUSD": "LINKUSDT",
    "LINK-USD": "LINKUSDT",
    "DOT": "DOTUSDT",
    "DOTUSD": "DOTUSDT",
    "DOT-USD": "DOTUSDT",
    "MATIC": "MATICUSDT",
    "MATICUSD": "MATICUSDT",
    "MATIC-USD": "MATICUSDT",
    "POL": "POLUSDT",
    "POLUSD": "POLUSDT",
    "POL-USD": "POLUSDT",
}

_ET = ZoneInfo("America/New_York")
_BJ = ZoneInfo("Asia/Shanghai")


def binance_enabled() -> bool:
    return os.environ.get("BINANCE_MARKET_DATA_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def binance_base_urls() -> list[str]:
    primary = (
        os.environ.get("BINANCE_DATA_API_BASE", "").strip()
        or "https://data-api.binance.vision"
    )
    fallback = (
        os.environ.get("BINANCE_API_BASE", "").strip() or "https://api.binance.com"
    )
    urls = [primary.rstrip("/")]
    if fallback.rstrip("/") not in urls:
        urls.append(fallback.rstrip("/"))
    return urls


def to_binance_symbol(ticker: str) -> str | None:
    """美股/通用 ticker → 币安现货交易对；无法映射则 None。"""
    raw = (ticker or "").strip().upper().replace("/", "").replace(" ", "")
    if not raw:
        return None
    if raw in _SYMBOL_ALIAS:
        return _SYMBOL_ALIAS[raw]
    # 已是 BTCUSDT / ETHBTC 等形式
    if raw.endswith("USDT") and len(raw) > 4 and raw.isalnum():
        return raw
    if raw.endswith("USD") and not raw.endswith("USDT") and len(raw) > 3:
        base = raw[:-3]
        if base.isalpha() and 2 <= len(base) <= 10:
            return f"{base}USDT"
    return None


def binance_mappable(symbols: list[str]) -> list[str]:
    return [
        s.upper().strip()
        for s in symbols
        if s and str(s).strip() and to_binance_symbol(str(s))
    ]


def _ms_to_quote_times(close_ms: int | None) -> tuple[str | None, str | None]:
    if not close_ms:
        return None, None
    try:
        dt = datetime.fromtimestamp(int(close_ms) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None, None
    return (
        dt.astimezone(_BJ).strftime("%Y-%m-%d %H:%M:%S"),
        dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
    )


async def _get_json(path: str, params: dict[str, Any] | None = None) -> Any | None:
    last_err: Exception | None = None
    async with httpx.AsyncClient(headers=_UA, timeout=18.0, follow_redirects=True) as client:
        for base in binance_base_urls():
            url = f"{base}{path}"
            try:
                resp = await client.get(url, params=params or {})
            except Exception as exc:
                last_err = exc
                logger.debug("Binance GET %s failed: %s", url, exc)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as exc:
                    last_err = exc
                    continue
            if resp.status_code == 429:
                logger.warning("Binance rate limited on %s", path)
                return None
            # -1121 invalid symbol 等：换域名也没用，直接停
            if resp.status_code in (400, 404):
                logger.debug("Binance %s HTTP %s: %s", path, resp.status_code, resp.text[:160])
                return None
            logger.debug("Binance %s HTTP %s via %s", path, resp.status_code, base)
    if last_err:
        logger.warning("Binance all bases failed for %s: %s", path, last_err)
    return None


def _row_from_24hr(app_symbol: str, row: dict[str, Any]) -> dict[str, Any] | None:
    from app.heatmap import _quote_row

    try:
        price = float(row.get("lastPrice") or row.get("weightedAvgPrice") or 0)
        chg = float(row.get("priceChangePercent") or 0)
        vol = float(row.get("volume") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    close_ms = row.get("closeTime")
    try:
        close_ms_i = int(close_ms) if close_ms is not None else None
    except (TypeError, ValueError):
        close_ms_i = None
    bj, et = _ms_to_quote_times(close_ms_i)
    return _quote_row(
        app_symbol,
        name=app_symbol,
        price=price,
        change_pct=round(chg, 2),
        volume=vol,
        quote_time=bj,
        quote_time_et=et,
    )


async def fetch_binance_quotes(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """批量 24h ticker；只处理可映射的加密货币符号。"""
    if not binance_enabled():
        return {}
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    pair_to_apps: dict[str, list[str]] = {}
    for sym in uniq:
        pair = to_binance_symbol(sym)
        if not pair:
            continue
        pair_to_apps.setdefault(pair, []).append(sym)
    if not pair_to_apps:
        return {}

    pairs = list(pair_to_apps.keys())
    out: dict[str, dict[str, Any]] = {}

    # 单对用 symbol=；多对用 symbols= JSON 数组（官方 rest-api）
    if len(pairs) == 1:
        data = await _get_json("/api/v3/ticker/24hr", {"symbol": pairs[0]})
        rows = [data] if isinstance(data, dict) else []
    else:
        # symbols=["BTCUSDT","ETHUSDT"]
        import json as _json

        data = await _get_json(
            "/api/v3/ticker/24hr",
            {"symbols": _json.dumps(pairs, separators=(",", ":"))},
        )
        rows = data if isinstance(data, list) else []

    for row in rows:
        if not isinstance(row, dict):
            continue
        pair = str(row.get("symbol") or "").upper()
        apps = pair_to_apps.get(pair) or []
        for app_sym in apps:
            parsed = _row_from_24hr(app_sym, row)
            if parsed:
                out[app_sym] = parsed
    if out:
        logger.info("Binance quotes hit %s/%s pairs", len(out), len(pairs))
    return out


async def fetch_binance_daily_closes(
    ticker: str, *, lookback_days: int = 60
) -> list[tuple[date, float]]:
    """日 K 收盘（UTC 日线）；不可映射则空列表。"""
    if not binance_enabled():
        return []
    pair = to_binance_symbol(ticker)
    if not pair:
        return []
    limit = max(3, min(int(lookback_days) + 5, 1000))
    data = await _get_json(
        "/api/v3/klines",
        {"symbol": pair, "interval": "1d", "limit": limit},
    )
    if not isinstance(data, list):
        return []
    rows: list[tuple[date, float]] = []
    for bar in data:
        if not isinstance(bar, (list, tuple)) or len(bar) < 5:
            continue
        try:
            open_ms = int(bar[0])
            close_px = float(bar[4])
        except (TypeError, ValueError):
            continue
        if close_px <= 0:
            continue
        d = datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc).date()
        rows.append((d, close_px))
    rows.sort(key=lambda x: x[0])
    return rows
