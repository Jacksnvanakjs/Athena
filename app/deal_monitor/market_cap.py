"""市值缓存与分档刷新（Yahoo Finance）。"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from app.database import MarketCapCache
from app.deal_monitor.config import FINNHUB_API_KEY
from app.deal_monitor.entities import Entity
from app.deal_monitor.tiers import classify_tier
from app.utils import now_beijing

logger = logging.getLogger(__name__)


async def _fetch_yahoo_market_cap(ticker: str) -> float | None:
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    params = {"modules": "price,defaultKeyStatistics"}
    headers = {"User-Agent": "Mozilla/5.0 AthenaDealMonitor/1.0"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            data = resp.json()
            result = data.get("quoteSummary", {}).get("result", [])
            if not result:
                return None
            price_mod = result[0].get("price", {})
            stats = result[0].get("defaultKeyStatistics", {})
            cap = price_mod.get("marketCap", {}).get("raw")
            if cap is None:
                cap = stats.get("marketCap", {}).get("raw")
            return float(cap) if cap else None
    except Exception as exc:
        logger.debug("Yahoo 市值 %s 失败: %s", ticker, exc)
        return None


async def _fetch_finnhub_market_cap(ticker: str) -> float | None:
    if not FINNHUB_API_KEY:
        return None
    url = "https://finnhub.io/api/v1/stock/profile2"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, params={"symbol": ticker, "token": FINNHUB_API_KEY})
            if resp.status_code != 200:
                return None
            data = resp.json()
            cap = data.get("marketCapitalization")
            if cap:
                return float(cap) * 1_000_000  # Finnhub 返回百万美元
    except Exception as exc:
        logger.debug("Finnhub 市值 %s 失败: %s", ticker, exc)
    return None


async def fetch_market_cap(ticker: str) -> float | None:
    cap = await _fetch_finnhub_market_cap(ticker)
    if cap is None:
        cap = await _fetch_yahoo_market_cap(ticker)
    return cap


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
