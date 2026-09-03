"""市值缓存与分档刷新：Finnhub / Yahoo 多源兜底。"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import FINNHUB_API_KEY
from app.database import MarketCapCache
from app.deal_monitor.entities import Entity
from app.deal_monitor.tiers import classify_tier
from app.utils import now_beijing

logger = logging.getLogger(__name__)

_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 AthenaDealMonitor/1.0"}


async def _fetch_finnhub_profile(ticker: str) -> dict:
    if not FINNHUB_API_KEY:
        return {}
    url = "https://finnhub.io/api/v1/stock/profile2"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url, params={"symbol": ticker.upper(), "token": FINNHUB_API_KEY}
            )
            if resp.status_code != 200:
                logger.warning("Finnhub profile %s HTTP %s", ticker, resp.status_code)
                return {}
            data = resp.json() or {}
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Finnhub profile %s 失败: %s", ticker, exc)
        return {}


async def _fetch_finnhub_quote_price(ticker: str) -> float | None:
    if not FINNHUB_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker.upper(), "token": FINNHUB_API_KEY},
            )
            if resp.status_code != 200:
                return None
            price = (resp.json() or {}).get("c")
            return float(price) if price else None
    except Exception as exc:
        logger.debug("Finnhub quote %s 失败: %s", ticker, exc)
        return None


async def _fetch_finnhub_market_cap(ticker: str) -> float | None:
    data = await _fetch_finnhub_profile(ticker)
    cap = data.get("marketCapitalization")
    if cap:
        return float(cap) * 1_000_000  # Finnhub 返回百万美元
    shares_m = data.get("shareOutstanding")
    if shares_m:
        price = await _fetch_finnhub_quote_price(ticker)
        if price:
            return float(shares_m) * 1_000_000 * price
    return None


async def _fetch_yahoo_market_cap(ticker: str) -> float | None:
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    params = {"modules": "price,defaultKeyStatistics,summaryDetail"}
    try:
        async with httpx.AsyncClient(headers=_YAHOO_HEADERS, timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            result = (resp.json().get("quoteSummary") or {}).get("result") or []
            if not result:
                return None
            price_mod = result[0].get("price") or {}
            stats = result[0].get("defaultKeyStatistics") or {}
            summary = result[0].get("summaryDetail") or {}
            for block in (price_mod, stats, summary):
                raw = (block.get("marketCap") or {}).get("raw")
                if raw:
                    return float(raw)
            shares = (stats.get("sharesOutstanding") or {}).get("raw")
            px = (price_mod.get("regularMarketPrice") or {}).get("raw")
            if shares and px:
                return float(shares) * float(px)
    except Exception as exc:
        logger.debug("Yahoo 市值 %s 失败: %s", ticker, exc)
    return None


async def _yahoo_daily_closes(
    ticker: str, *, lookback_days: int = 120
) -> list[tuple[date, float]]:
    """兼容旧名：内部走全站多源日线（Yahoo→Stooq→AKShare）。"""
    from app.market_data import fetch_daily_closes

    return await fetch_daily_closes(ticker, lookback_days=lookback_days)


async def _shares_outstanding(ticker: str) -> float | None:
    """流通/总股本（股数）。优先 Finnhub，再 Yahoo。"""
    data = await _fetch_finnhub_profile(ticker)
    shares_m = data.get("shareOutstanding")
    if shares_m:
        return float(shares_m) * 1_000_000
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    try:
        async with httpx.AsyncClient(headers=_YAHOO_HEADERS, timeout=15) as client:
            resp = await client.get(
                url, params={"modules": "defaultKeyStatistics"}
            )
            if resp.status_code != 200:
                return None
            result = (resp.json().get("quoteSummary") or {}).get("result") or []
            if not result:
                return None
            raw = (
                (result[0].get("defaultKeyStatistics") or {})
                .get("sharesOutstanding")
                or {}
            ).get("raw")
            return float(raw) if raw else None
    except Exception as exc:
        logger.debug("Yahoo shares %s 失败: %s", ticker, exc)
        return None


async def _estimate_cap_from_price(
    ticker: str, price: float | None
) -> float | None:
    if not price or price <= 0:
        return None
    shares = await _shares_outstanding(ticker)
    if not shares:
        return None
    return shares * price


async def fetch_market_cap(ticker: str) -> float | None:
    """实时市值：Finnhub → Yahoo quoteSummary → 股价×股本估算。"""
    ticker = ticker.upper()
    for name, coro in (
        ("finnhub", _fetch_finnhub_market_cap(ticker)),
        ("yahoo_summary", _fetch_yahoo_market_cap(ticker)),
    ):
        try:
            cap = await coro
        except Exception as exc:
            logger.warning("市值源 %s/%s 异常: %s", ticker, name, exc)
            cap = None
        if cap and cap > 0:
            logger.info("市值 %s 来自 %s: %.0f", ticker, name, cap)
            return float(cap)

    closes = await _yahoo_daily_closes(ticker, lookback_days=10)
    if closes:
        cap = await _estimate_cap_from_price(ticker, closes[-1][1])
        if cap:
            logger.info("市值 %s 来自 chart×股本: %.0f", ticker, cap)
            return cap
    logger.warning("市值 %s 全部来源失败", ticker)
    return None


def _close_on_or_before(
    closes: list[tuple[date, float]], as_of: date
) -> float | None:
    best: float | None = None
    for d, cl in closes:
        if d <= as_of:
            best = cl
        else:
            break
    return best


async def fetch_market_cap_as_of(ticker: str, as_of: date) -> float | None:
    """按指定交易日（含）之前最近收盘价 × 股本估算市值。

    用于历史回填：股本用当前可得口径（变动慢），价格严格用 as_of 及之前。
    """
    ticker = ticker.upper()
    closes = await _yahoo_daily_closes(ticker, lookback_days=90)
    price = _close_on_or_before(closes, as_of)
    if price is None:
        logger.warning("市值 as_of %s/%s 无收盘价", ticker, as_of)
        return None
    cap = await _estimate_cap_from_price(ticker, price)
    if cap:
        logger.info(
            "市值 %s as_of %s: price=%.2f cap=%.0f", ticker, as_of, price, cap
        )
    return cap


async def fetch_pre_30d_gain_as_of(
    ticker: str, as_of: date
) -> float | None:
    """相对 as_of 约 30 日前收盘的涨幅（小数）。价格窗口截止 as_of，不含之后。"""
    closes = await _yahoo_daily_closes(ticker, lookback_days=90)
    capped = [(d, cl) for d, cl in closes if d <= as_of]
    if len(capped) < 5:
        return None
    lookback = min(21, len(capped) - 1)
    old = capped[-(lookback + 1)][1]
    latest = capped[-1][1]
    if not old or old <= 0:
        return None
    return (latest - old) / old


def get_cached_market_cap(db: Session, ticker: str, max_age_hours: int = 24) -> float | None:
    row = db.query(MarketCapCache).filter(MarketCapCache.ticker == ticker.upper()).first()
    if not row:
        return None
    if row.refreshed_at < now_beijing() - timedelta(hours=max_age_hours):
        return None
    return row.market_cap_usd


def save_market_cap_cache(db: Session, ticker: str, cap: float, tier: str) -> None:
    ticker = ticker.upper()
    row = db.query(MarketCapCache).filter(MarketCapCache.ticker == ticker).first()
    now = now_beijing()
    if row:
        row.market_cap_usd = cap
        row.tier = tier
        row.refreshed_at = now
    else:
        db.add(MarketCapCache(ticker=ticker, market_cap_usd=cap, tier=tier, refreshed_at=now))


async def enrich_entity_tiers(db: Session, entities: list[Entity]) -> None:
    for entity in entities:
        if entity.unlisted_id:
            entity.tier = "T0"
            continue
        if not entity.ticker:
            entity.tier = "UNKNOWN"
            continue
        ticker = entity.ticker.upper()
        cached = get_cached_market_cap(db, ticker)
        if cached is not None:
            entity.market_cap_usd = cached
        else:
            cap = await fetch_market_cap(ticker)
            if cap is not None:
                entity.market_cap_usd = cap
                tier = classify_tier(cap, ticker=ticker)
                save_market_cap_cache(db, ticker, cap, tier)
                db.commit()
        entity.tier = classify_tier(entity.market_cap_usd, ticker=ticker, unlisted_id=entity.unlisted_id)


async def refresh_all_seed_market_caps(db: Session) -> int:
    """日更：刷新种子库中所有 ticker 的市值分档。"""
    from app.deal_monitor.entities import registry

    registry.load_seed()
    tickers = {t for _, t, u in registry._aliases if t and not u}
    tickers.update(registry._t0_listed)
    count = 0
    for ticker in sorted(tickers):
        cap = await fetch_market_cap(ticker)
        if cap is None:
            continue
        tier = classify_tier(cap, ticker=ticker)
        save_market_cap_cache(db, ticker, cap, tier)
        count += 1
    db.commit()
    return count
