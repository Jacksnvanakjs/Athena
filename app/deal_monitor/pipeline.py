"""抓取 → 解析 → 打分 → 去重 → 入库 → 推送。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from app.database import DealEvent, DealSeenUrl, db_session
from app.seen_url_cache import TtlBox, deal_seen_urls
from app.deal_monitor.config import (
    DEAL_DEDUP_DAYS,
    DEAL_HIDE_WEAK_QUALITY,
    DEAL_INGEST_MAX_AGE_DAYS,
    DEAL_LLM_MIN_SCORE,
    DEAL_LLM_MODEL,
    DEAL_MAX_PUSH_PER_BENEFICIARY_24H,
    DEAL_MAX_PUSH_PER_HOUR,
    DEAL_POLL_INTERVAL_MIN,
    DEAL_PUSH_ENABLED,
    DEAL_PUSH_MAX_AGE_DAYS,
    DEAL_PUSH_RETRY_FRESH_MIN,
    DEAL_STORY_DEDUP_DAYS,
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
from app.deal_monitor.content_filter import (
    deal_amount_keys,
    should_hide_deal_event,
    should_hide_weak_quality_event,
)
from app.deal_monitor.first_day import try_fill_first_day_now
from app.deal_monitor.story_fingerprint import is_related_deal_story, is_same_story
from app.deal_monitor.ingest_policy import (
    llm_allows_beneficiary_ingest,
    llm_primary_enabled,
    pre_llm_reject,
    should_skip_vague_despite_llm,
)
from app.deal_monitor.keywords import is_product_only_integration, is_update_headline, passes_keyword_filter
from app.deal_monitor.llm_classifier import LlmDecision, classify_items, classify_one
from app.deal_monitor.market_cap import enrich_entity_tiers
from app.market_data.tradability import is_us_tradable
from app.deal_monitor.materiality import (
    QUALITY_FINANCING,
    QUALITY_HARD,
    QUALITY_SOFT_PRODUCT,
    QUALITY_VAGUE,
    classify_deal_quality,
    finalize_materiality_score,
    should_soft_skip_push,
)
from app.deal_monitor.parser import infer_partnership_pair, infer_partnership_pair_text
from app.deal_monitor.tiers import RoleAssignment, assign_roles, score_threshold
from app.notifier import notify, successful_channels
from app.push_format import build_deal_push_content
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


def _anchor_matches_prior(prior: DealEvent, anchor_key: str | None) -> bool:
    if not anchor_key:
        return False
    ak = anchor_key.upper()
    if prior.anchor_ticker and prior.anchor_ticker.upper() == ak:
        return True
    name = (prior.anchor_name or "").strip()
    if name and (name.lower() == anchor_key.lower() or name.upper() == ak):
        return True
    return False


def _dedup_blocked(
    db: Session,
    beneficiary_ticker: str,
    is_update: bool,
    anchor_key: str | None = None,
    headline: str | None = None,
    summary: str | None = None,
) -> bool:
    """同一受益方+锚点 7 日内不重复；30 日内同故事（指纹/容量/合作线索）也不重复。"""
    if is_update:
        return False
    since_hard = now_beijing() - timedelta(days=DEAL_DEDUP_DAYS)
    since_story = now_beijing() - timedelta(days=max(DEAL_STORY_DEDUP_DAYS, DEAL_DEDUP_DAYS))
    blob = f"{headline or ''}\n{summary or ''}"

    q = db.query(DealEvent).filter(
        DealEvent.beneficiary_ticker == beneficiary_ticker,
        DealEvent.fetched_at >= since_hard,
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

    priors = (
        db.query(DealEvent)
        .filter(
            DealEvent.beneficiary_ticker == beneficiary_ticker,
            DealEvent.fetched_at >= since_story,
            DealEvent.is_update.is_(False),
        )
        .all()
    )
    amounts = deal_amount_keys(blob)
    for prior in priors:
        same_anchor = _anchor_matches_prior(prior, anchor_key)
        if is_related_deal_story(
            headline,
            summary,
            prior.headline,
            prior.summary,
            url_a=None,
            url_b=prior.source_url,
            same_anchor=same_anchor,
        ):
            return True
        if amounts and amounts & deal_amount_keys(
            f"{prior.headline or ''}\n{prior.summary or ''}"
        ):
            return True
    return False


def _push_succeeded(event: DealEvent) -> bool:
    ch = (event.push_channel or "").strip()
    return bool(
        event.pushed_at
        and ch
        and ch not in {"none", "failed", "unconfigured", "disabled", "rate_limited", "stale", "soft_skip", "tier_skip"}
    )


def _push_rate_limited(db: Session, event: DealEvent) -> bool:
    """限流：0=关闭对应闸门。

    - 小时总上限：全库成功推送条数（DEAL_MAX_PUSH_PER_HOUR）
    - 受益方：STORY_DEDUP 窗内已成功推送过同/相关故事则不重复推
    """
    ticker = (event.beneficiary_ticker or "").strip().upper()
    since_story = now_beijing() - timedelta(days=max(DEAL_STORY_DEDUP_DAYS, DEAL_DEDUP_DAYS))
    since_1h = now_beijing() - timedelta(hours=1)
    success_like = and_(
        DealEvent.push_channel.isnot(None),
        ~DealEvent.push_channel.in_(
            ["none", "failed", "unconfigured", "disabled", "rate_limited", "stale", "soft_skip", "tier_skip"]
        ),
    )

    if ticker and DEAL_STORY_DEDUP_DAYS > 0:
        priors = (
            db.query(DealEvent)
            .filter(
                DealEvent.beneficiary_ticker == ticker,
                DealEvent.pushed_at.isnot(None),
                DealEvent.pushed_at >= since_story,
                success_like,
            )
            .all()
        )
        anchor_key = _anchor_dedup_key(
            getattr(event, "anchor_ticker", None),
            getattr(event, "anchor_name", None),
        )
        for prior in priors:
            if is_related_deal_story(
                event.headline,
                event.summary,
                prior.headline,
                prior.summary,
                url_a=event.source_url,
                url_b=prior.source_url,
                same_anchor=_anchor_matches_prior(prior, anchor_key),
            ):
                return True

    if DEAL_MAX_PUSH_PER_HOUR > 0:
        global_count = (
            db.query(DealEvent)
            .filter(
                DealEvent.pushed_at.isnot(None),
                DealEvent.pushed_at >= since_1h,
                success_like,
            )
            .count()
        )
        if global_count >= DEAL_MAX_PUSH_PER_HOUR:
            return True
    return False


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
    """仅在渠道真正发送成功时写入 pushed_at；失败可被后续短窗口单条重试。"""
    if not roles_should_push or not DEAL_PUSH_ENABLED:
        event.push_channel = "disabled"
        return
    if not _passes_web_display_rules(event):
        event.push_channel = "soft_skip"
        event.pushed_at = None
        logger.info("不符合网页展示规则，不推送 %s", event.beneficiary_ticker)
        return
    if _push_rate_limited(db, event):
        logger.info("推送频率限制，跳过 %s", event.beneficiary_ticker)
        event.push_channel = "rate_limited"
        return

    title, content = build_deal_push_content(event)
    results = await notify(title, content)
    if not results:
        logger.warning("推送通道未配置（BARK/PUSHPLUS 为空），跳过 %s", event.beneficiary_ticker)
    await _apply_notify_result(event, results)


def _passes_web_display_rules(event: DealEvent) -> bool:
    """推送与网页默认列表共用：隐藏规则命中则不推。"""
    if is_test_source_url(getattr(event, "source_url", None)):
        return False
    if should_hide_deal_event(event):
        return False
    if DEAL_HIDE_WEAK_QUALITY and should_hide_weak_quality_event(event):
        return False
    return True


def _retry_fresh_cutoff() -> datetime:
    mins = max(DEAL_PUSH_RETRY_FRESH_MIN, DEAL_POLL_INTERVAL_MIN * 2)
    return now_beijing() - timedelta(minutes=mins)


async def retry_unpushed_events(db: Session, limit: int = 3) -> int:
    """仅补推刚抓取且仍符合展示规则的失败/限流单条。

    - 禁止积压合并（digest）扎堆推送
    - 超过新鲜窗口的失败/限流标记为 stale，不再补发
    """
    if not DEAL_PUSH_ENABLED:
        return 0

    fresh_since = _retry_fresh_cutoff()
    stale_marked = 0
    for event in (
        db.query(DealEvent)
        .filter(
            DealEvent.push_channel.in_(
                ["none", "failed", "unconfigured", "rate_limited"]
            ),
            or_(
                DealEvent.fetched_at.is_(None),
                DealEvent.fetched_at < fresh_since,
            ),
        )
        .limit(80)
        .all()
    ):
        # 已过新鲜窗口：放弃补推，避免稍后扎堆
        event.push_channel = "stale"
        event.pushed_at = None
        db.add(event)
        stale_marked += 1
    if stale_marked:
        db.commit()
        logger.info("放弃过期补推 %s 条（超过新鲜窗口或不及时）", stale_marked)

    candidates = (
        db.query(DealEvent)
        .filter(
            DealEvent.fetched_at >= fresh_since,
            DealEvent.push_channel.in_(
                ["none", "failed", "unconfigured", "rate_limited"]
            ),
        )
        .order_by(DealEvent.fetched_at.desc(), DealEvent.id.desc())
        .limit(20)
        .all()
    )

    pushed = 0
    for event in candidates:
        if pushed >= limit:
            break
        if _published_too_stale_for_push(event.published_at):
            event.push_channel = "stale"
            event.pushed_at = None
            db.add(event)
            continue
        if not _passes_web_display_rules(event):
            event.push_channel = "soft_skip"
            event.pushed_at = None
            db.add(event)
            continue
        if _push_rate_limited(db, event):
            # 仍限流则保持 rate_limited，等下一轮新鲜窗口内再试；不合并多条
            event.push_channel = "rate_limited"
            db.add(event)
            break
        await _maybe_push(db, event, roles_should_push=True)
        db.add(event)
        if _push_succeeded(event):
            pushed += 1
    db.commit()
    return pushed


def _save_event(
    db: Session,
    item: RawItem,
    roles,
    score: int,
    matched_keywords: list[str],
    is_update: bool,
    beneficiary: Entity,
    event_type: str = EVENT_TYPE,
    *,
    fetched_at: datetime | None = None,
) -> DealEvent:
    from app.deal_monitor.headline_zh import build_zh_headline

    h_hash = headline_hash(item.headline)
    display_headline = build_zh_headline(
        beneficiary.ticker or "",
        item.headline,
        item.summary,
    )
    now = now_beijing()
    event = DealEvent(
        published_at=item.published_at.replace(tzinfo=None),
        fetched_at=fetched_at or now,
        headline=display_headline[:500],
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
        scored_at=now,
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


async def process_item(
    db: Session,
    item: RawItem,
    llm_decision: LlmDecision | None = None,
    *,
    fetched_at: datetime | None = None,
) -> dict:
    stats = {"skipped": True, "reason": ""}
    if is_test_source_url(item.source_url):
        stats["reason"] = "测试/占位链接，不入库"
        return stats
    reject, reject_reason = pre_llm_reject(item)
    if reject:
        stats["reason"] = f"内容过滤: {reject_reason}"
        return stats
    text = f"{item.headline}\n{item.summary}"
    matched: list[str] = []
    primary = llm_primary_enabled()

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
            if _same_company(anchor, benef):
                logger.info(
                    "跳过同公司角色: anchor=%s beneficiary=%s",
                    anchor.ticker or anchor.name,
                    benef.ticker or benef.name,
                )
                continue
            roles = assign_roles(anchor, benef)
            if not roles:
                continue
            if _same_company(roles.anchor, roles.beneficiary):
                continue
            hard_ok = (
                classify_deal_quality(text) == QUALITY_HARD and bool(benef.ticker)
            )
            llm_ok = primary and llm_allows_beneficiary_ingest(
                llm_decision, has_ticker=bool(benef.ticker)
            )
            if roles.should_push or hard_ok or llm_ok:
                role_pairs.append((roles, benef))
        if not role_pairs:
            stats["reason"] = "LLM 受益方规则不推送（或锚点=受益方）"
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
        # T0↔T0 / 无 ticker 等默认不推：硬催化仍可入库对照（如 PLTR×PwC）
        quality_early = classify_deal_quality(text)
        if not roles.should_push:
            listed_benef = bool(roles.beneficiary and roles.beneficiary.ticker)
            if not (quality_early == QUALITY_HARD and listed_benef):
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
            if not role_pairs and roles.beneficiary and roles.beneficiary.ticker:
                role_pairs = [(roles, roles.beneficiary)]

    quality = classify_deal_quality(text)
    # 空话合作：LLM 主导时仅低分丢弃；旧模式一律不入库

    event_type = (llm_decision.event_type if llm_decision else None) or EVENT_TYPE
    is_update = is_update_headline(item.headline)
    saved: list[str] = []
    last_score = 0
    last_tier_pair = ""

    if llm_decision and should_skip_vague_despite_llm(text, llm_decision):
        stats["reason"] = "空话合作/无商业条款，不入库"
        return stats
    if not llm_decision and quality == QUALITY_VAGUE:
        stats["reason"] = "空话合作/无商业条款，不入库"
        return stats

    for roles, beneficiary in role_pairs:
        score = finalize_materiality_score(
            text,
            item.source,
            matched,
            llm_score=(llm_decision.llm_score if llm_decision else None),
            event_type=event_type,
            llm_primary=primary if DEAL_USE_LLM else False,
        )
        if (
            llm_decision
            and llm_decision.llm_score
            and llm_decision.llm_score < DEAL_LLM_MIN_SCORE
            and quality not in (QUALITY_SOFT_PRODUCT, QUALITY_FINANCING, QUALITY_VAGUE)
        ):
            stats["reason"] = (
                f"LLM 材料性不足 {llm_decision.llm_score} < {DEAL_LLM_MIN_SCORE}"
            )
            return stats
        threshold = score_threshold(roles.tier_pair)
        # LLM 主导：门槛取 min(档位门槛, LLM 最低分)，避免规则门槛否决高分 LLM
        if primary and llm_decision and llm_decision.llm_score:
            effective_threshold = min(threshold, DEAL_LLM_MIN_SCORE)
        elif quality in (QUALITY_SOFT_PRODUCT, QUALITY_FINANCING):
            effective_threshold = max(55, threshold)
        else:
            effective_threshold = threshold
        # 软整合/融资：更高入库门槛（弱催化少占列表）；仍 soft_skip 不推送
        if quality in (QUALITY_SOFT_PRODUCT, QUALITY_FINANCING) and not primary:
            effective_threshold = max(55, threshold)
        if score < effective_threshold:
            stats["reason"] = f"材料性 {score} < {effective_threshold}"
            continue

        ticker = (beneficiary.ticker or "").upper()
        if not ticker:
            continue
        if not await is_us_tradable(ticker):
            stats["reason"] = f"受益方 {ticker} 无美股可买行情（退市/停牌）"
            logger.info("跳过不可交易受益方 %s", ticker)
            continue
        anchor_key = _anchor_dedup_key(roles.anchor.ticker, roles.anchor.name)
        if _is_duplicate(db, item.source_url, h_hash, ticker):
            stats["reason"] = "URL/标题去重"
            continue
        if _dedup_blocked(
            db,
            ticker,
            is_update,
            anchor_key=anchor_key,
            headline=item.headline,
            summary=item.summary,
        ):
            logger.info("7/30 天故事去重跳过 %s (anchor=%s)", ticker, anchor_key)
            continue

        should_push = roles.should_push and not should_soft_skip_push(quality)

        event = _save_event(
            db,
            item,
            roles,
            score,
            matched,
            is_update,
            beneficiary,
            event_type=event_type,
            fetched_at=fetched_at,
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
            if should_soft_skip_push(quality):
                event.push_channel = "soft_skip"
            elif not roles.should_push:
                event.push_channel = "tier_skip"
            else:
                event.push_channel = "soft_skip"
            event.pushed_at = None
        elif not _passes_web_display_rules(event):
            event.push_channel = "soft_skip"
            event.pushed_at = None
            logger.info("网页隐藏规则命中，入库不推送 %s", ticker)
        else:
            await _maybe_push(db, event, True)
        await try_fill_first_day_now(event)
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


# 同轮多条新闻并行处理上限（LLM+入库+推送互不等待）
_PROCESS_CONCURRENCY = max(1, int(os.getenv("DEAL_PROCESS_CONCURRENCY", "2")))
_pushed_count_cache = TtlBox(900)


async def _claim_and_filter_items(
    items: list[RawItem],
    *,
    claimed: set[str],
    claim_lock: asyncio.Lock,
) -> tuple[list[tuple[RawItem, datetime]], int, int]:
    """去重+硬过滤；返回 (item, discovered_at)、内容拒数、过旧数。"""
    discovered: list[tuple[RawItem, datetime]] = []
    content_rejected = 0
    stale_dropped = 0
    now = now_beijing()

    async with claim_lock:
        with db_session() as db:
            pending: list[tuple[RawItem, str]] = []
            for item in items:
                url = (item.source_url or "").strip()
                if not url or url in claimed:
                    continue
                pending.append((item, url))
            _, unseen = deal_seen_urls.partition(
                db, DealSeenUrl, [url for _, url in pending]
            )
            marked: list[str] = []
            for item, url in pending:
                if url not in unseen:
                    continue
                claimed.add(url)
                if _published_too_stale_for_ingest(item.published_at):
                    stale_dropped += 1
                    db.merge(
                        DealSeenUrl(
                            source_url=url,
                            headline_hash=headline_hash(item.headline),
                            seen_at=now,
                            llm_relevant=False,
                        )
                    )
                    marked.append(url)
                    continue
                reject, reason = pre_llm_reject(item)
                if reject:
                    content_rejected += 1
                    logger.info("内容过滤跳过: %s — %s", item.headline[:80], reason)
                    db.merge(
                        DealSeenUrl(
                            source_url=url,
                            headline_hash=headline_hash(item.headline),
                            seen_at=now,
                            llm_relevant=False,
                        )
                    )
                    marked.append(url)
                    continue
                # discovered_at：源侧刚拿到的时间，不被 LLM/兄弟稿拖后
                discovered.append((item, now_beijing()))
            if marked:
                db.commit()
                deal_seen_urls.add_many(marked)
    return discovered, content_rejected, stale_dropped


async def _ingest_one(
    item: RawItem,
    discovered_at: datetime,
    *,
    sem: asyncio.Semaphore,
) -> dict:
    """单条：独立 LLM → 入库 → 推送，不阻塞同轮其他稿。"""
    async with sem:
        decision: LlmDecision | None = None
        if DEAL_USE_LLM:
            decision = await classify_one(item)
            if decision is None:
                # API 失败不写 seen，下一轮重试
                return {"skipped": True, "reason": "llm_failed", "retry": True}

        with db_session() as db:
            try:
                result = await process_item(
                    db, item, decision, fetched_at=discovered_at
                )
                db.merge(
                    DealSeenUrl(
                        source_url=item.source_url,
                        headline_hash=headline_hash(item.headline),
                        seen_at=now_beijing(),
                        llm_relevant=bool(decision and decision.is_relevant),
                    )
                )
                db.commit()
                deal_seen_urls.add(item.source_url)
                return result
            except Exception as exc:
                logger.exception("处理条目失败: %s", item.headline[:80])
                db.rollback()
                return {"skipped": True, "reason": str(exc)[:200], "error": True}


async def _handle_source_batch(
    fetch_coro,
    *,
    claimed: set[str],
    claim_lock: asyncio.Lock,
    sem: asyncio.Semaphore,
) -> dict:
    """某一抓取源完成后立刻过滤并并行 ingest，不等其他源。"""
    stats = {
        "fetched": 0,
        "fetched_new": 0,
        "content_filtered": 0,
        "stale_dropped": 0,
        "processed": 0,
        "saved": 0,
        "errors": [],
        "llm_hits": 0,
    }
    try:
        items: list[RawItem] = await fetch_coro
    except Exception as exc:
        logger.exception("抓取源失败")
        stats["errors"].append(str(exc)[:200])
        return stats

    # 源内 URL 去重
    uniq: list[RawItem] = []
    seen_local: set[str] = set()
    for item in items:
        url = (item.source_url or "").strip()
        if not url or url in seen_local:
            continue
        seen_local.add(url)
        uniq.append(item)
    stats["fetched"] = len(uniq)

    discovered, rejected, stale = await _claim_and_filter_items(
        uniq, claimed=claimed, claim_lock=claim_lock
    )
    stats["content_filtered"] = rejected
    stats["stale_dropped"] = stale
    stats["fetched_new"] = len(discovered)

    if not discovered:
        return stats

    # 新稿优先，但仍并行，不串行等旧稿
    discovered.sort(
        key=lambda pair: (
            pair[0].published_at.replace(tzinfo=None)
            if pair[0].published_at and pair[0].published_at.tzinfo
            else pair[0].published_at
        )
        or datetime.min,
        reverse=True,
    )

    results = await asyncio.gather(
        *[_ingest_one(item, discovered_at, sem=sem) for item, discovered_at in discovered],
        return_exceptions=True,
    )
    for res in results:
        if isinstance(res, Exception):
            stats["errors"].append(str(res)[:200])
            continue
        if res.get("error"):
            stats["errors"].append(res.get("reason") or "error")
            continue
        if res.get("retry"):
            continue
        if not res.get("skipped"):
            stats["processed"] += 1
            stats["saved"] += len(res.get("saved") or [])
            stats["llm_hits"] += 1
    return stats


async def run_pipeline() -> dict:
    """多源并行抓取；每源一到就立刻并行分类/入库/推送，稿件互不等待。"""
    registry.load_seed()
    claimed: set[str] = set()
    claim_lock = asyncio.Lock()
    sem = asyncio.Semaphore(_PROCESS_CONCURRENCY)

    with db_session() as db:
        registry.sync_to_db(db)

    parts = await asyncio.gather(
        _handle_source_batch(
            fetch_pr_wires(), claimed=claimed, claim_lock=claim_lock, sem=sem
        ),
        _handle_source_batch(
            fetch_company_ir_feeds(), claimed=claimed, claim_lock=claim_lock, sem=sem
        ),
        _handle_source_batch(
            fetch_finnhub_and_google(), claimed=claimed, claim_lock=claim_lock, sem=sem
        ),
        _handle_source_batch(
            fetch_sec_8k(), claimed=claimed, claim_lock=claim_lock, sem=sem
        ),
        return_exceptions=True,
    )

    summary: dict = {
        "fetched": 0,
        "fetched_pr": 0,
        "fetched_ir": 0,
        "fetched_agg": 0,
        "fetched_sec_8k": 0,
        "fetched_new": 0,
        "stale_dropped": 0,
        "content_filtered": 0,
        "processed": 0,
        "saved": 0,
        "pushed": 0,
        "errors": [],
        "llm_enabled": DEAL_USE_LLM,
        "llm_model": DEAL_LLM_MODEL if DEAL_USE_LLM else None,
        "llm_hits": 0,
        "parallel": True,
        "process_concurrency": _PROCESS_CONCURRENCY,
    }
    labels = ("fetched_pr", "fetched_ir", "fetched_agg", "fetched_sec_8k")
    for label, part in zip(labels, parts):
        if isinstance(part, Exception):
            summary["errors"].append(str(part)[:200])
            continue
        summary[label] = part.get("fetched", 0)
        summary["fetched"] += part.get("fetched", 0)
        summary["fetched_new"] += part.get("fetched_new", 0)
        summary["stale_dropped"] += part.get("stale_dropped", 0)
        summary["content_filtered"] += part.get("content_filtered", 0)
        summary["processed"] += part.get("processed", 0)
        summary["saved"] += part.get("saved", 0)
        summary["llm_hits"] += part.get("llm_hits", 0)
        summary["errors"].extend(part.get("errors") or [])

    with db_session() as db:
        summary["push_retried"] = await retry_unpushed_events(db)

        def _count_pushed():
            return (
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

        summary["pushed"] = int(_pushed_count_cache.get(_count_pushed) or 0)

    logger.info("deal_monitor pipeline: %s", summary)
    return summary
