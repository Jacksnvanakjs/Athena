"""NVDA A 档信号：抓取 → 分类 → 打分 → 去重 → 入库 → 推送。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import (
    DEAL_MAX_PUSH_PER_BENEFICIARY_24H,
    DEAL_MAX_PUSH_PER_HOUR,
    NVDA_SIGNAL_A_DEDUP_DAYS,
    NVDA_SIGNAL_A_PLUS_B_DEDUP_DAYS,
    NVDA_SIGNAL_A_PLUS_B_ENABLED,
    NVDA_SIGNAL_ENABLED,
    NVDA_SIGNAL_MIN_MATERIALITY_A,
    NVDA_SIGNAL_MIN_MATERIALITY_A_PLUS_B,
    NVDA_SIGNAL_PUSH_ENABLED,
    NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A,
    NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A_PLUS_B,
)
from app.database import NvdaSignalEvent, NvdaSignalSeenUrl, db_session
from app.deal_monitor.entities import Entity, registry
from app.deal_monitor.entity_resolver import is_channel_partner_entity, resolve_entity
from app.deal_monitor.fetchers.pr_wire import RawItem
from app.deal_monitor.market_cap import enrich_entity_tiers
from app.deal_monitor.pipeline import normalize_headline, _published_too_stale_for_ingest
from app.nvda_signal.classifier import classify_signal
from app.nvda_signal.config import ACTION_MIN_SCORE
from app.nvda_signal.fetchers import fetch_all_nvda_items
from app.nvda_signal.keywords import has_acquire_terms, has_nvda, is_rumor, _norm
from app.nvda_signal.materiality import score_a, score_a_plus_b
from app.nvda_signal.prior_a_lookup import find_prior_a, prior_a_days_ago
from app.nvda_signal.trade_window import build_trade_plan
from app.notifier import notify
from app.push_format import build_nvda_push_content
from app.source_url_guard import is_test_source_url
from app.text_clean import clean_article_text
from app.utils import now_beijing

logger = logging.getLogger(__name__)

NVDA_TICKERS = {"NVDA", "NVIDIA"}


def headline_hash(headline: str) -> str:
    return hashlib.md5(normalize_headline(headline).encode()).hexdigest()


def _entity_in_headline(entity: Entity, headline: str) -> bool:
    """受益方须在标题中出现，避免摘要/URL 误匹配。"""
    h = headline.lower()
    name = (entity.name or "").lower()
    if name and name in h:
        return True
    ticker = (entity.ticker or "").upper()
    if ticker and re.search(rf"\b{re.escape(ticker)}\b", headline, re.I):
        return True
    registry.load_seed()
    for alias, tick, uid in registry._aliases:
        if tick != entity.ticker and uid != entity.unlisted_id:
            continue
        if alias in h or (len(alias) <= 4 and re.search(rf"\b{re.escape(alias)}\b", h)):
            return True
    return False


def _unlisted_target_in_headline(headline: str) -> bool:
    """标题主标的若为未上市公司（如 Hugging Face）。"""
    registry.load_seed()
    h = headline.lower()
    for alias, ticker, uid in registry._aliases:
        if not uid or ticker:
            continue
        if len(alias) >= 5 and alias in h:
            return True
        if len(alias) <= 4 and re.search(rf"\b{re.escape(alias)}\b", h):
            return True
    return False


def _is_nvda_acquire_unlisted(headline: str, text: str) -> bool:
    """NVDA 官宣收购未上市标的：受益方记 NVDA 自身。"""
    blob = _norm(f"{headline}\n{text}")
    if not has_nvda(blob) or not has_acquire_terms(blob):
        return False
    return _unlisted_target_in_headline(headline) or _unlisted_target_in_headline(text)


def _nvda_as_beneficiary() -> Entity:
    return Entity(name="NVIDIA", ticker="NVDA", tier="T0")


def _extract_beneficiaries(text: str, headline: str) -> list[Entity]:
    registry.load_seed()
    clean = clean_article_text(text)
    found: dict[str, Entity] = {}
    for entity in registry.extract_entities(clean):
        ticker = (entity.ticker or "").upper()
        if ticker in NVDA_TICKERS or not ticker:
            continue
        if entity.unlisted_id:
            continue
        if is_channel_partner_entity(entity, clean):
            continue
        if not _entity_in_headline(entity, headline):
            continue
        found[ticker] = entity
    return list(found.values())


def _allow_t0_beneficiary(entity: Entity, action_type: str) -> bool:
    """仅 NVDA 收购未上市巨头时允许 T0（NVDA）入库。"""
    return (
        (entity.ticker or "").upper() == "NVDA"
        and action_type == "NVDA_ACQUIRE_UNLISTED"
    )


def _is_t0(entity: Entity) -> bool:
    return entity.tier == "T0" or registry.is_t0_listed_seed(entity.ticker)


def _dedup_blocked(
    db: Session,
    ticker: str,
    signal_tier: str,
    action_type: str,
) -> bool:
    days = NVDA_SIGNAL_A_DEDUP_DAYS if signal_tier == "A" else NVDA_SIGNAL_A_PLUS_B_DEDUP_DAYS
    since = now_beijing() - timedelta(days=days)
    q = db.query(NvdaSignalEvent).filter(
        NvdaSignalEvent.beneficiary_ticker == ticker.upper(),
        NvdaSignalEvent.fetched_at >= since,
    )
    if signal_tier == "A":
        q = q.filter(NvdaSignalEvent.action_type == action_type)
    else:
        q = q.filter(NvdaSignalEvent.signal_tier == "A_PLUS_B")
    return q.first() is not None


def _push_rate_limited(db: Session, event: NvdaSignalEvent) -> bool:
    """限流：0=关闭。受益方仅拦相同 URL/正文指纹，不同新闻可继续推。"""
    from sqlalchemy import and_

    from app.deal_monitor.story_fingerprint import is_same_story

    ticker = (event.beneficiary_ticker or "").strip().upper()
    since_24h = now_beijing() - timedelta(hours=24)
    since_1h = now_beijing() - timedelta(hours=1)
    success_like = and_(
        NvdaSignalEvent.push_channel.isnot(None),
        ~NvdaSignalEvent.push_channel.in_(
            ["none", "failed", "unconfigured", "disabled", "rate_limited", "stale", "soft_skip", "tier_skip"]
        ),
    )

    if DEAL_MAX_PUSH_PER_BENEFICIARY_24H > 0 and ticker:
        priors = (
            db.query(NvdaSignalEvent)
            .filter(
                NvdaSignalEvent.beneficiary_ticker == ticker,
                NvdaSignalEvent.pushed_at.isnot(None),
                NvdaSignalEvent.pushed_at >= since_24h,
                success_like,
            )
            .all()
        )
        same = 0
        for prior in priors:
            if is_same_story(
                event.headline,
                event.summary,
                prior.headline,
                prior.summary,
                url_a=event.source_url,
                url_b=prior.source_url,
            ):
                same += 1
                if same >= DEAL_MAX_PUSH_PER_BENEFICIARY_24H:
                    return True

    if DEAL_MAX_PUSH_PER_HOUR > 0:
        c1 = (
            db.query(NvdaSignalEvent)
            .filter(
                NvdaSignalEvent.pushed_at.isnot(None),
                NvdaSignalEvent.pushed_at >= since_1h,
                success_like,
            )
            .count()
        )
        if c1 >= DEAL_MAX_PUSH_PER_HOUR:
            return True
    return False


async def _maybe_push(db: Session, event: NvdaSignalEvent) -> None:
    if not NVDA_SIGNAL_PUSH_ENABLED:
        event.push_channel = "disabled"
        return

    min_conf = (
        NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A_PLUS_B
        if event.signal_tier == "A_PLUS_B"
        else NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A
    )
    if event.confidence < min_conf:
        event.push_channel = "none"
        return

    if _push_rate_limited(db, event):
        event.push_channel = "rate_limited"
        return

    title, content = build_nvda_push_content(event)
    results = await notify(title, content)
    if not results:
        event.push_channel = "unconfigured"
        event.pushed_at = None
        return

    channels = [k for k, ok in results.items() if ok]
    if channels:
        event.push_channel = ",".join(channels)
        event.pushed_at = now_beijing()
    else:
        event.push_channel = "failed"
        event.pushed_at = None


async def process_item(
    db: Session,
    item: RawItem,
    *,
    fetched_at: datetime | None = None,
) -> dict:
    stats = {"skipped": True, "reason": ""}
    if is_test_source_url(item.source_url):
        stats["reason"] = "测试/占位链接，不入库"
        return stats

    headline = item.headline or ""
    if is_rumor(_norm(headline)):
        stats["reason"] = "标题含传闻措辞，非官宣"
        return stats

    text = f"{headline}\n{item.summary}"
    acquire_unlisted = _is_nvda_acquire_unlisted(headline, text)

    if _unlisted_target_in_headline(headline) and not acquire_unlisted:
        stats["reason"] = "交易标的未上市，无可操作美股"
        return stats

    clean_text = clean_article_text(text)
    beneficiaries = _extract_beneficiaries(text, headline)
    if not beneficiaries and acquire_unlisted:
        beneficiaries = [_nvda_as_beneficiary()]
    if not beneficiaries:
        stats["reason"] = "未识别美股受益方"
        return stats

    saved: list[str] = []
    for raw_entity in beneficiaries:
        entity = await resolve_entity(raw_entity.name, context=text)
        if not entity.ticker and acquire_unlisted and (raw_entity.ticker or "").upper() == "NVDA":
            entity = raw_entity
        if not entity.ticker:
            continue

        prior = find_prior_a(db, entity.ticker)
        classification = classify_signal(clean_text, has_prior_a=prior is not None)
        if not classification:
            stats["reason"] = "不符合 NVDA 信号语义"
            continue

        if acquire_unlisted and classification.signal_tier == "A":
            classification.action_type = "NVDA_ACQUIRE_UNLISTED"

        if classification.signal_tier in ("B", "C"):
            stats["reason"] = f"{classification.signal_tier}档仅日志"
            continue

        if classification.signal_tier == "A_PLUS_B" and not NVDA_SIGNAL_A_PLUS_B_ENABLED:
            stats["reason"] = "A_PLUS_B 已关闭"
            continue

        if classification.status == "rumor":
            stats["reason"] = "传闻无官方跟进"
            continue

        if classification.signal_tier == "A_PLUS_B":
            if not prior or prior.status != "confirmed":
                stats["reason"] = "无有效前期 A 档"
                continue
            if classification.beneficiary_role != "direct":
                stats["reason"] = "A_PLUS_B 仅推 direct"
                continue

        await enrich_entity_tiers(db, [entity])
        if _is_t0(entity) and not _allow_t0_beneficiary(entity, classification.action_type):
            stats["reason"] = "T0 受益方不推送"
            continue

        blob = clean_text.lower()
        if classification.signal_tier == "A":
            materiality, confidence = score_a(blob, item.source, classification.action_type)
            min_mat = max(NVDA_SIGNAL_MIN_MATERIALITY_A, ACTION_MIN_SCORE.get(classification.action_type, 65))
        else:
            days = prior_a_days_ago(prior) if prior else 999
            materiality, confidence = score_a_plus_b(blob, item.source, prior, days)
            min_mat = NVDA_SIGNAL_MIN_MATERIALITY_A_PLUS_B

        if materiality < min_mat:
            stats["reason"] = f"材料性 {materiality} < {min_mat}"
            continue

        if _dedup_blocked(db, entity.ticker, classification.signal_tier, classification.action_type):
            stats["reason"] = "去重窗口内重复"
            continue

        if db.query(NvdaSignalEvent).filter(NvdaSignalEvent.source_url == item.source_url,
                                            NvdaSignalEvent.beneficiary_ticker == entity.ticker.upper()).first():
            stats["reason"] = "URL+ticker 已存在"
            continue

        plan = build_trade_plan(classification.signal_tier, item.published_at)
        from app.deal_monitor.headline_zh import build_zh_headline

        event = NvdaSignalEvent(
            published_at=item.published_at.replace(tzinfo=None),
            fetched_at=fetched_at or now_beijing(),
            headline=build_zh_headline(
                entity.ticker or "",
                item.headline,
                item.summary,
            )[:500],
            summary=item.summary,
            source=item.source,
            source_url=item.source_url,
            headline_hash=headline_hash(item.headline),
            beneficiary_ticker=entity.ticker.upper(),
            beneficiary_name=entity.name,
            beneficiary_tier=entity.tier,
            beneficiary_market_cap_usd=entity.market_cap_usd,
            beneficiary_role=classification.beneficiary_role,
            signal_tier=classification.signal_tier,
            action_type=classification.action_type,
            materiality_score=materiality,
            scored_at=now_beijing(),
            confidence=confidence,
            status=classification.status,
            strategy=plan.strategy,
            buy_window=plan.buy_window,
            sell_window=plan.sell_window,
            sell_plan_json=plan.sell_plan_json,
            prior_a_event_id=prior.id if prior and classification.signal_tier == "A_PLUS_B" else None,
            prior_a_days_ago=prior_a_days_ago(prior) if prior and classification.signal_tier == "A_PLUS_B" else None,
            position_pct=plan.position_pct,
            buy_ok=plan.buy_ok,
            chase_risk=plan.chase_risk,
        )
        db.add(event)
        await _maybe_push(db, event)
        saved.append(entity.ticker.upper())

    if saved:
        stats["skipped"] = False
        stats["saved"] = saved
    return stats


_NVDA_PROCESS_CONCURRENCY = max(1, int(os.getenv("DEAL_PROCESS_CONCURRENCY", "6")))


async def _nvda_ingest_one(
    item: RawItem,
    discovered_at: datetime,
    *,
    sem: asyncio.Semaphore,
) -> dict:
    async with sem:
        with db_session() as db:
            try:
                result = await process_item(db, item, fetched_at=discovered_at)
                db.merge(
                    NvdaSignalSeenUrl(
                        source_url=item.source_url,
                        headline_hash=headline_hash(item.headline),
                        seen_at=now_beijing(),
                        relevant=not result.get("skipped"),
                    )
                )
                db.commit()
                return result
            except Exception as exc:
                logger.exception("NVDA 信号处理失败: %s", item.headline[:80])
                db.rollback()
                return {"skipped": True, "reason": str(exc)[:200], "error": True}


async def run_pipeline() -> dict:
    if not NVDA_SIGNAL_ENABLED:
        return {"enabled": False, "fetched": 0, "saved": 0}

    registry.load_seed()
    items = await fetch_all_nvda_items()
    summary = {
        "enabled": True,
        "fetched": len(items),
        "fetched_new": 0,
        "saved": 0,
        "pushed": 0,
        "errors": [],
        "parallel": True,
    }

    with db_session() as db:
        seen = {r.source_url for r in db.query(NvdaSignalSeenUrl.source_url).all()}
        new_items = [i for i in items if i.source_url not in seen and not is_test_source_url(i.source_url)]
        summary["fetched_new"] = len(new_items)

        stale_dropped = 0
        eligible: list[tuple[RawItem, datetime]] = []
        for item in new_items:
            if _published_too_stale_for_ingest(item.published_at):
                stale_dropped += 1
                db.merge(
                    NvdaSignalSeenUrl(
                        source_url=item.source_url,
                        headline_hash=headline_hash(item.headline),
                        seen_at=now_beijing(),
                        relevant=False,
                    )
                )
                continue
            eligible.append((item, now_beijing()))
        if stale_dropped:
            db.commit()
            summary["stale_dropped"] = stale_dropped

    eligible.sort(
        key=lambda pair: (
            pair[0].published_at.replace(tzinfo=None)
            if pair[0].published_at and pair[0].published_at.tzinfo
            else pair[0].published_at
        )
        or datetime.min,
        reverse=True,
    )

    sem = asyncio.Semaphore(_NVDA_PROCESS_CONCURRENCY)
    results = await asyncio.gather(
        *[_nvda_ingest_one(item, discovered, sem=sem) for item, discovered in eligible],
        return_exceptions=True,
    )
    for res in results:
        if isinstance(res, Exception):
            summary["errors"].append(str(res)[:200])
            continue
        if res.get("error"):
            summary["errors"].append(res.get("reason") or "error")
            continue
        if not res.get("skipped"):
            summary["saved"] += len(res.get("saved") or [])

    with db_session() as db:
        summary["pushed"] = (
            db.query(NvdaSignalEvent)
            .filter(
                NvdaSignalEvent.pushed_at.isnot(None),
                NvdaSignalEvent.push_channel.isnot(None),
                ~NvdaSignalEvent.push_channel.in_(
                    ["none", "failed", "unconfigured", "disabled", "rate_limited"]
                ),
            )
            .count()
        )

    logger.info("nvda_signal pipeline: %s", summary)
    return summary
