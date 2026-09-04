"""抓取 → 解析 → 打分 → 去重 → 入库 → 推送。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from app.database import DealEvent, DealSeenUrl, db_session
from app.deal_monitor.config import (
    DEAL_DEDUP_DAYS,
    DEAL_INGEST_MAX_AGE_DAYS,
    DEAL_LLM_MODEL,
    DEAL_MAX_PUSH_PER_BENEFICIARY_24H,
    DEAL_MAX_PUSH_PER_HOUR,
    DEAL_PUSH_ENABLED,
    DEAL_PUSH_MAX_AGE_DAYS,
    DEAL_USE_LLM,
)
from app.deal_monitor.entities import Entity, registry
from app.deal_monitor.entity_resolver import (
    is_channel_partner_entity,
    parse_sec_filer,
    resolve_entity,
)
from app.deal_monitor.fetchers.pr_wire import RawItem, fetch_pr_wires
from app.deal_monitor.fetchers.sec_edgar import fetch_sec_8k
from app.deal_monitor.fetchers.company_ir import fetch_finnhub_and_google
from app.deal_monitor.fetchers.company_ir_rss import fetch_company_ir_feeds
from app.deal_monitor.content_filter import deal_amount_keys, reject_deal_item
from app.deal_monitor.keywords import is_product_only_integration, is_update_headline, passes_keyword_filter
from app.deal_monitor.llm_classifier import LlmDecision, classify_items
from app.deal_monitor.market_cap import enrich_entity_tiers
from app.deal_monitor.materiality import (
    QUALITY_FINANCING,
    QUALITY_SOFT_PRODUCT,
    classify_deal_quality,
    finalize_materiality_score,
)
from app.deal_monitor.parser import infer_partnership_pair, infer_partnership_pair_text
from app.deal_monitor.tiers import RoleAssignment, assign_roles, score_threshold
from app.notifier import notify, successful_channels
from app.push_format import build_deal_digest_push_content, build_deal_push_content
from app.source_url_guard import is_test_source_url
from app.utils import now_beijing

logger = logging.getLogger(__name__)

EVENT_TYPE = "compute_deal"


def normalize_headline(headline: str) -> str:
    text = headline.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text)


def headline_hash(headline: str) -> str:
    return hashlib.md5(normalize_headline(headline).encode()).hexdigest()


def _as_utc_aware(published_at: datetime) -> datetime:
    if published_at.tzinfo is None:
        return published_at.replace(tzinfo=timezone.utc)
    return published_at.astimezone(timezone.utc)


def _published_age_days(published_at: datetime) -> float:
    return (datetime.now(timezone.utc) - _as_utc_aware(published_at)).total_seconds() / 86400


def _published_too_stale_for_ingest(published_at: datetime) -> bool:
    """超过 DEAL_INGEST_MAX_AGE_DAYS 则不入库、不进 LLM（消假 lag）。"""
    if DEAL_INGEST_MAX_AGE_DAYS <= 0:
        return False
    return _published_age_days(published_at) > DEAL_INGEST_MAX_AGE_DAYS


def _published_too_stale_for_push(published_at: datetime) -> bool:
    """published_at 为 UTC naive；超过 DEAL_PUSH_MAX_AGE_DAYS 则不推送。"""
    if DEAL_PUSH_MAX_AGE_DAYS <= 0:
        return False
    return _published_age_days(published_at) > DEAL_PUSH_MAX_AGE_DAYS


def _same_company(a: Entity, b: Entity) -> bool:
    if a.ticker and b.ticker and a.ticker.upper() == b.ticker.upper():
        return True
    if a.unlisted_id and b.unlisted_id and a.unlisted_id == b.unlisted_id:
        return True
    na = (a.name or "").strip().lower()
    nb = (b.name or "").strip().lower()
    return bool(na and nb and na == nb)


def _exclude_channel_partners(entities: list[Entity], context: str) -> list[Entity]:
    return [e for e in entities if not is_channel_partner_entity(e, context)]


def _is_duplicate(db: Session, url: str, h_hash: str, beneficiary_ticker: str) -> bool:
    if (
        db.query(DealEvent)
        .filter(
            DealEvent.source_url == url,
            DealEvent.beneficiary_ticker == beneficiary_ticker,
        )
        .first()
    ):
        return True
    if (
        db.query(DealEvent)
        .filter(
            DealEvent.headline_hash == h_hash,
            DealEvent.beneficiary_ticker == beneficiary_ticker,
        )
        .first()
    ):
        return True
    return False


def _anchor_dedup_key(anchor_ticker: str | None, anchor_name: str | None) -> str:
    if anchor_ticker:
        return anchor_ticker.upper()
    return (anchor_name or "").strip().lower()


def _dedup_blocked(
    db: Session,
    beneficiary_ticker: str,
    is_update: bool,
    anchor_key: str | None = None,
    headline: str | None = None,
) -> bool:
    """同一受益方+锚点 7 日内不重复；同受益方+同金额故事也不重复（防换锚点洗稿）。"""
    if is_update:
        return False
    since = now_beijing() - timedelta(days=DEAL_DEDUP_DAYS)
    q = db.query(DealEvent).filter(
        DealEvent.beneficiary_ticker == beneficiary_ticker,
        DealEvent.fetched_at >= since,
        DealEvent.is_update.is_(False),
    )
    if anchor_key:
        ak = anchor_key.upper()
        q = q.filter(
            or_(
                DealEvent.anchor_ticker == ak,
                DealEvent.anchor_name.ilike(anchor_key),
                DealEvent.anchor_name.ilike(ak),
            )
        )
    if q.first() is not None:
        return True

    amounts = deal_amount_keys(headline or "")
    if not amounts:
        return False
    priors = (
        db.query(DealEvent)
        .filter(
            DealEvent.beneficiary_ticker == beneficiary_ticker,
            DealEvent.fetched_at >= since,
            DealEvent.is_update.is_(False),
        )
        .all()
    )
    for prior in priors:
        if amounts & deal_amount_keys(prior.headline or ""):
            return True
    return False


def _push_succeeded(event: DealEvent) -> bool:
    ch = (event.push_channel or "").strip()
    return bool(
        event.pushed_at
        and ch
        and ch not in {"none", "failed", "unconfigured", "disabled", "rate_limited", "stale", "soft_skip"}
    )


def _push_rate_limited(db: Session, beneficiary_ticker: str) -> bool:
    since_24h = now_beijing() - timedelta(hours=24)
    since_1h = now_beijing() - timedelta(hours=1)
    # 只统计真正推成功的，避免失败占额度、页面误显示已推送
    success_like = and_(
        DealEvent.push_channel.isnot(None),
        ~DealEvent.push_channel.in_(
            ["none", "failed", "unconfigured", "disabled", "rate_limited"]
        ),
    )
    count_24h = (
        db.query(DealEvent)
        .filter(
            DealEvent.beneficiary_ticker == beneficiary_ticker,
            DealEvent.pushed_at.isnot(None),
            DealEvent.pushed_at >= since_24h,
            success_like,
        )
        .count()
    )
    if count_24h >= DEAL_MAX_PUSH_PER_BENEFICIARY_24H:
        return True
    global_count = (
        db.query(DealEvent)
        .filter(
            DealEvent.pushed_at.isnot(None),
            DealEvent.pushed_at >= since_1h,
            success_like,
        )
        .count()
    )
    return global_count >= DEAL_MAX_PUSH_PER_HOUR


async def _apply_notify_result(event: DealEvent, results: dict) -> bool:
    """根据 notify 结果写 pushed_at / push_channel。成功返回 True。"""
    if not results:
        event.push_channel = "unconfigured"
        event.pushed_at = None
        return False
    channels = successful_channels(results)
    if channels:
        event.pushed_at = now_beijing()
        event.push_channel = "+".join(channels)
        logger.info("已推送 %s via %s", event.beneficiary_ticker, event.push_channel)
        return True
    event.push_channel = "failed"
    event.pushed_at = None
    logger.warning("推送全部失败 %s results=%s", event.beneficiary_ticker, results)
    return False


async def _maybe_push(db: Session, event: DealEvent, roles_should_push: bool) -> None:
    """仅在渠道真正发送成功时写入 pushed_at；失败可被后续重试。"""
    if not roles_should_push or not DEAL_PUSH_ENABLED:
        event.push_channel = "disabled"
        return
    if _push_rate_limited(db, event.beneficiary_ticker):
        logger.info("推送频率限制，跳过 %s", event.beneficiary_ticker)
        event.push_channel = "rate_limited"
        return

    title, content = build_deal_push_content(event)
    results = await notify(title, content)
    if not results:
        logger.warning("推送通道未配置（BARK/PUSHPLUS 为空），跳过 %s", event.beneficiary_ticker)
    await _apply_notify_result(event, results)


async def retry_unpushed_events(db: Session, limit: int = 12) -> int:
    """补推失败/限流事件。

    积压 ≥2 条时合并成一条综合推送（占 1 次额度），避免通道日限额恢复后连发。
    仅 1 条时仍单条即时补推。
    """
    if not DEAL_PUSH_ENABLED:
        return 0
    candidates = (
        db.query(DealEvent)
        .filter(
            or_(
                DealEvent.pushed_at.is_(None),
                DealEvent.push_channel.is_(None),
                DealEvent.push_channel.in_(
                    ["none", "failed", "unconfigured", "rate_limited"]
                ),
            )
        )
        .order_by(DealEvent.fetched_at.desc(), DealEvent.id.desc())
        .limit(40)
        .all()
    )

    eligible: list[DealEvent] = []
    touched = False
    seen_ticker: set[str] = set()
    for event in candidates:
        if _published_too_stale_for_push(event.published_at):
            event.push_channel = "stale"
            event.pushed_at = None
            db.add(event)
            touched = True
            continue
        ticker = (event.beneficiary_ticker or "").strip().upper()
        if ticker and ticker in seen_ticker:
            event.push_channel = "soft_skip"
            event.pushed_at = None
            db.add(event)
            touched = True
            continue
        if _push_rate_limited(db, event.beneficiary_ticker):
            event.push_channel = "rate_limited"
            db.add(event)
            touched = True
            continue
        if ticker:
            seen_ticker.add(ticker)
        eligible.append(event)
        if len(eligible) >= limit:
            break

    if not eligible:
        if touched:
            db.commit()
        return 0

    if len(eligible) == 1:
        await _maybe_push(db, eligible[0], roles_should_push=True)
        db.add(eligible[0])
        db.commit()
        return 1 if _push_succeeded(eligible[0]) else 0

    title, content = build_deal_digest_push_content(eligible)
    results = await notify(title, content)
    if not results:
        logger.warning("综合推送通道未配置，跳过 %s 条积压", len(eligible))
        for event in eligible:
            event.push_channel = "unconfigured"
            event.pushed_at = None
            db.add(event)
        db.commit()
        return 0

    channels = successful_channels(results)
    if not channels:
        logger.warning("综合推送全部失败 results=%s", results)
        for event in eligible:
            event.push_channel = "failed"
            event.pushed_at = None
            db.add(event)
        db.commit()
        return 0

    channel = "+".join(channels) + "+digest"
    now = now_beijing()
    for event in eligible:
        event.pushed_at = now
        event.push_channel = channel
        db.add(event)
    db.commit()
    logger.info(
        "已综合推送 %s 条积压 via %s: %s",
        len(eligible),
        channel,
        ", ".join(e.beneficiary_ticker or "?" for e in eligible),
    )
    return len(eligible)


def _save_event(
    db: Session,
    item: RawItem,
    roles,
    score: int,
    matched_keywords: list[str],
    is_update: bool,
    beneficiary: Entity,
    event_type: str = EVENT_TYPE,
) -> DealEvent:
    h_hash = headline_hash(item.headline)
    event = DealEvent(
        published_at=item.published_at.replace(tzinfo=None),
        fetched_at=now_beijing(),
        headline=item.headline,
        summary=item.summary,
        source=item.source,
        source_url=item.source_url,
        headline_hash=h_hash,
        anchor_name=roles.anchor.name,
        anchor_ticker=roles.anchor.ticker if roles.anchor.ticker else None,
        anchor_tier=roles.anchor.tier,
        beneficiary_ticker=beneficiary.ticker.upper(),
        beneficiary_name=beneficiary.name,
        beneficiary_tier=beneficiary.tier,
        beneficiary_market_cap_usd=beneficiary.market_cap_usd,
        tier_pair=roles.tier_pair,
        materiality_score=score,
        matched_keywords=json.dumps(matched_keywords, ensure_ascii=False),
        event_type=event_type or EVENT_TYPE,
        is_update=is_update,
    )
    db.add(event)
    return event


async def _resolve_llm_anchor_and_beneficiaries(
    db: Session,
    llm_decision: LlmDecision,
    text: str,
) -> tuple[Entity | None, list[Entity], str | None]:
    """LLM 指定锚点 + 一个或多个美股受益方（含产业链间接受益）。"""
    anchor_name = (llm_decision.anchor_name or "").strip()
    anchor = await resolve_entity(anchor_name, context=text) if anchor_name else Entity(name="")
    if anchor.is_unknown:
        for ent in registry.extract_entities(text):
            if ent.unlisted_id or (ent.ticker and registry.is_t0_listed_seed(ent.ticker)):
                anchor = ent
                break
    if anchor.is_unknown:
        return None, [], "LLM 锚点无法解析"

    beneficiaries: list[Entity] = []
    seen: set[str] = set()
    for name in llm_decision.all_beneficiary_names():
        ent = await resolve_entity(name, context=text)
        if not ent.ticker:
            continue
        tick = ent.ticker.upper()
        if tick in seen:
            continue
        seen.add(tick)
        beneficiaries.append(ent)

    if not beneficiaries:
        return anchor, [], "LLM 受益方无美股代码"

    await enrich_entity_tiers(db, [anchor, *beneficiaries])
    return anchor, beneficiaries, None


async def process_item(db: Session, item: RawItem, llm_decision: LlmDecision | None = None) -> dict:
    stats = {"skipped": True, "reason": ""}
    if is_test_source_url(item.source_url):
        stats["reason"] = "测试/占位链接，不入库"
        return stats
    reject, reject_reason = reject_deal_item(item)
    if reject:
        stats["reason"] = f"内容过滤: {reject_reason}"
        return stats
    text = f"{item.headline}\n{item.summary}"
    matched: list[str] = []

    if DEAL_USE_LLM:
        if not llm_decision:
            stats["reason"] = "LLM 无判定"
            return stats
        if not llm_decision.is_relevant:
            stats["reason"] = f"LLM 判定不相关: {llm_decision.reason}"
            return stats
        matched = ["LLM"]
        if llm_decision and llm_decision.reason:
            snippet = llm_decision.reason.strip()[:160]
            if snippet:
                matched.append(snippet)
    else:
        ok, matched = passes_keyword_filter(text, source=item.source)
        if not ok:
            stats["reason"] = "关键词/合作词未通过"
            return stats

    h_hash = headline_hash(item.headline)
    role_pairs: list[tuple[RoleAssignment, Entity]] = []

    if DEAL_USE_LLM and llm_decision and llm_decision.is_relevant:
        if not llm_decision.all_beneficiary_names():
            stats["reason"] = "LLM 未给出美股受益方"
            return stats
        anchor, beneficiaries, err = await _resolve_llm_anchor_and_beneficiaries(
            db, llm_decision, text
        )
        if err:
            stats["reason"] = err
            return stats
        assert anchor is not None
        for benef in beneficiaries:
            if is_channel_partner_entity(benef, text):
                continue
            roles = assign_roles(anchor, benef)
            if roles and roles.should_push:
                role_pairs.append((roles, benef))
        if not role_pairs:
            stats["reason"] = "LLM 受益方规则不推送"
            return stats
    else:
        entity_a: Entity | None = None
        entity_b: Entity | None = None
        if item.source == "sec_8k":
            filer = parse_sec_filer(item.headline)
            if not filer:
                stats["reason"] = "SEC 8-K 未解析申报方"
                return stats
            entity_a = await resolve_entity(filer, context=text)
            entities = _exclude_channel_partners(
                [e for e in registry.extract_entities(text) if not _same_company(e, entity_a)],
                text,
            )
            if entities:
                pair = infer_partnership_pair(item.headline, item.summary, [entity_a, *entities])
                entity_b = pair[1] if pair else entities[0]
                if not entity_b.ticker:
                    entity_b = await resolve_entity(entity_b.name, context=text)
            else:
                pair_text = infer_partnership_pair_text(item.headline, item.summary)
                if not pair_text:
                    stats["reason"] = "SEC 8-K 未识别到协议对方"
                    return stats
                _, b_name = pair_text
                entity_b = await resolve_entity(b_name, context=text)
        else:
            entities = _exclude_channel_partners(registry.extract_entities(text), text)
            if len(entities) < 2:
                pair_text = infer_partnership_pair_text(item.headline, item.summary)
                if not pair_text:
                    stats["reason"] = "未识别到合作双方"
                    return stats
                a_name, b_name = pair_text
                entity_a = await resolve_entity(a_name, context=text)
                entity_b = await resolve_entity(b_name, context=text)
            else:
                pair = infer_partnership_pair(item.headline, item.summary, entities)
                if not pair:
                    pair_text = infer_partnership_pair_text(item.headline, item.summary)
                    if not pair_text:
                        stats["reason"] = "无法推断合作对"
                        return stats
                    a_name, b_name = pair_text
                    entity_a = await resolve_entity(a_name, context=text)
                    entity_b = await resolve_entity(b_name, context=text)
                else:
                    entity_a, entity_b = pair
                    if not entity_a.ticker:
                        entity_a = await resolve_entity(entity_a.name, context=text)
                    if not entity_b.ticker:
                        entity_b = await resolve_entity(entity_b.name, context=text)

        if not entity_a or not entity_b:
            stats["reason"] = "未识别到合作双方"
            return stats
        if _same_company(entity_a, entity_b):
            stats["reason"] = "双方为同一公司，不是合作事件"
            return stats
        if is_channel_partner_entity(entity_a, text) or is_channel_partner_entity(entity_b, text):
            stats["reason"] = "含渠道商/分销商，非合作方"
            return stats
        if is_product_only_integration(text):
            stats["reason"] = "纯产品整合/功能发布，无新商业条款"
            return stats

        await enrich_entity_tiers(db, [entity_a, entity_b])
        roles = assign_roles(entity_a, entity_b)
        if not roles:
            stats["reason"] = "角色判定失败"
            return stats
        if not roles.should_push:
            stats["reason"] = roles.skip_reason or "规则不推送"
            return stats

        role_pairs = [(roles, roles.beneficiary)]
        if roles.push_both:
            role_pairs = []
            for ent in (entity_a, entity_b):
                if ent.ticker:
                    r = assign_roles(entity_a, entity_b)
                    if r and r.should_push:
                        role_pairs.append((r, ent))

    quality = classify_deal_quality(text)
    # 软整合不再被 LLM 豁免成高分：下方 finalize 封顶且不推送

    event_type = (llm_decision.event_type if llm_decision else None) or EVENT_TYPE
    is_update = is_update_headline(item.headline)
    saved: list[str] = []
    last_score = 0
    last_tier_pair = ""

    for roles, beneficiary in role_pairs:
        score = finalize_materiality_score(
            text,
            item.source,
            matched,
            llm_score=(llm_decision.llm_score if llm_decision else None),
            event_type=event_type,
        )
        if (
            llm_decision
            and llm_decision.llm_score
            and llm_decision.llm_score < 70
            and quality not in (QUALITY_SOFT_PRODUCT, QUALITY_FINANCING)
        ):
            stats["reason"] = f"LLM 材料性不足 {llm_decision.llm_score} < 70"
            return stats
        threshold = score_threshold(roles.tier_pair)
        # 软整合/融资：用更低展示门槛，便于回测对照；但不推送
        effective_threshold = 50 if quality in (QUALITY_SOFT_PRODUCT, QUALITY_FINANCING) else threshold
        if score < effective_threshold:
            stats["reason"] = f"材料性 {score} < {effective_threshold}"
            continue

        ticker = (beneficiary.ticker or "").upper()
        if not ticker:
            continue
        anchor_key = _anchor_dedup_key(roles.anchor.ticker, roles.anchor.name)
        if _is_duplicate(db, item.source_url, h_hash, ticker):
            stats["reason"] = "URL/标题去重"
            continue
        if _dedup_blocked(db, ticker, is_update, anchor_key=anchor_key, headline=item.headline):
            logger.info("7 天去重跳过 %s (anchor=%s)", ticker, anchor_key)
            continue

        should_push = roles.should_push and quality not in (
            QUALITY_SOFT_PRODUCT,
            QUALITY_FINANCING,
        )

        event = _save_event(
            db, item, roles, score, matched, is_update, beneficiary, event_type=event_type
        )
        if _published_too_stale_for_push(event.published_at):
            event.push_channel = "stale"
            event.pushed_at = None
            logger.info(
                "发稿过旧，入库不推送 %s published=%s age_limit=%sd",
                ticker,
                event.published_at,
                DEAL_PUSH_MAX_AGE_DAYS,
            )
        elif not should_push:
            event.push_channel = "soft_skip"
            event.pushed_at = None
        else:
            await _maybe_push(db, event, True)
        saved.append(event.beneficiary_ticker)
        last_score = score
        last_tier_pair = roles.tier_pair

    if not saved:
        stats["reason"] = stats.get("reason") or "去重或未推送"
        return stats

    db.commit()
    stats["skipped"] = False
    stats["saved"] = saved
    stats["score"] = last_score
    stats["tier_pair"] = last_tier_pair
    return stats


async def run_pipeline() -> dict:
    """执行一轮 RSS / Finnhub / Google News / SEC 抓取与处理。"""
    registry.load_seed()
    pr_items, ir_items, agg_items, sec_items = await asyncio.gather(
        fetch_pr_wires(),
        fetch_company_ir_feeds(),
        fetch_finnhub_and_google(),
        fetch_sec_8k(),
    )
    # URL 去重：同一通稿可能同时出现在 PRN / BW / IR / Finnhub / Google
    items: list[RawItem] = []
    seen_fetch: set[str] = set()
    for batch in (pr_items, ir_items, agg_items, sec_items):
        for item in batch:
            url = (item.source_url or "").strip()
            if not url or url in seen_fetch:
                continue
            seen_fetch.add(url)
            items.append(item)
    summary = {
        "fetched": len(items),
        "fetched_pr": len(pr_items),
        "fetched_ir": len(ir_items),
        "fetched_agg": len(agg_items),
        "fetched_sec_8k": len(sec_items),
        "fetched_new": 0,
        "stale_dropped": 0,
        "processed": 0,
        "saved": 0,
        "pushed": 0,
        "errors": [],
    }
    llm_decisions: dict[str, LlmDecision] = {}

    with db_session() as db:
        registry.sync_to_db(db)
        seen_urls = {
            row.source_url
            for row in db.query(DealSeenUrl.source_url).all()
        }
        new_items = [item for item in items if item.source_url not in seen_urls]
        summary["fetched_new"] = len(new_items)

        content_rejected = 0
        stale_dropped = 0
        eligible_items: list[RawItem] = []
        for item in new_items:
            if _published_too_stale_for_ingest(item.published_at):
                stale_dropped += 1
                db.merge(
                    DealSeenUrl(
                        source_url=item.source_url,
                        headline_hash=headline_hash(item.headline),
                        seen_at=now_beijing(),
                        llm_relevant=False,
                    )
                )
                continue
            reject, reason = reject_deal_item(item)
            if reject:
                content_rejected += 1
                logger.info("内容过滤跳过: %s — %s", item.headline[:80], reason)
                db.merge(
                    DealSeenUrl(
                        source_url=item.source_url,
                        headline_hash=headline_hash(item.headline),
                        seen_at=now_beijing(),
                        llm_relevant=False,
                    )
                )
                continue
            eligible_items.append(item)
        if content_rejected or stale_dropped:
            db.commit()
            summary["content_filtered"] = content_rejected
            summary["stale_dropped"] = stale_dropped
        new_items = eligible_items

        if DEAL_USE_LLM:
            llm_decisions = await classify_items(new_items)
            summary["llm_enabled"] = True
            summary["llm_model"] = DEAL_LLM_MODEL
            summary["llm_hits"] = sum(1 for d in llm_decisions.values() if d.is_relevant)
            summary["llm_items_sent"] = len(new_items)
        else:
            summary["llm_enabled"] = False

        for item in new_items:
            try:
                result = await process_item(db, item, llm_decisions.get(item.source_url))
                if DEAL_USE_LLM and item.source_url not in llm_decisions:
                    # API 失败时不记 seen，下一轮重试，避免漏稿
                    continue
                db.merge(
                    DealSeenUrl(
                        source_url=item.source_url,
                        headline_hash=headline_hash(item.headline),
                        seen_at=now_beijing(),
                        llm_relevant=bool(
                            llm_decisions.get(item.source_url)
                            and llm_decisions[item.source_url].is_relevant
                        ),
                    )
                )
                db.commit()
                if not result.get("skipped"):
                    summary["processed"] += 1
                    summary["saved"] += len(result.get("saved", []))
            except Exception as exc:
                logger.exception("处理条目失败: %s", item.headline[:80])
                summary["errors"].append(str(exc)[:200])
                db.rollback()

        summary["push_retried"] = await retry_unpushed_events(db)
        summary["pushed"] = (
            db.query(DealEvent)
            .filter(
                DealEvent.pushed_at.isnot(None),
                DealEvent.push_channel.isnot(None),
                ~DealEvent.push_channel.in_(
                    ["none", "failed", "unconfigured", "disabled", "rate_limited"]
                ),
            )
            .count()
        )

    logger.info("deal_monitor pipeline: %s", summary)
    return summary
