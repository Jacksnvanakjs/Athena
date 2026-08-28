"""抓取 → 解析 → 打分 → 去重 → 入库 → 推送。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import timedelta

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from app.database import DealEvent, DealSeenUrl, db_session
from app.deal_monitor.config import (
    DEAL_DEDUP_DAYS,
    DEAL_LLM_MODEL,
    DEAL_MAX_PUSH_PER_BENEFICIARY_24H,
    DEAL_MAX_PUSH_PER_HOUR,
    DEAL_PUSH_ENABLED,
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
from app.deal_monitor.fetchers.company_ir import fetch_company_ir_and_aggregators
from app.deal_monitor.keywords import is_update_headline, passes_keyword_filter
from app.deal_monitor.llm_classifier import LlmDecision, classify_items
from app.deal_monitor.market_cap import enrich_entity_tiers
from app.deal_monitor.materiality import score_materiality
from app.deal_monitor.parser import infer_partnership_pair, infer_partnership_pair_text
from app.deal_monitor.tiers import assign_roles, score_threshold
from app.notifier import notify
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


def _cap_billions(cap: float | None) -> str:
    if cap is None:
        return "N/A"
    return f"{cap / 1e9:.1f}"


def build_push_content(
    event: DealEvent,
    anchor_ticker_display: str,
) -> tuple[str, str]:
    title = (
        f"[AI合作] {event.beneficiary_ticker} ← {event.anchor_name} "
        f"({event.tier_pair}) 分{event.materiality_score}"
    )
    cap_b = _cap_billions(event.beneficiary_market_cap_usd)
    keywords = event.matched_keywords or "[]"
    try:
        kw_list = json.loads(keywords)
        kw_str = ", ".join(kw_list) if kw_list else keywords
    except json.JSONDecodeError:
        kw_str = keywords

    lines = [
        "<b>🔔 AI 产业链合作快讯</b><br><br>",
        f"<b>受益</b>：{event.beneficiary_name} "
        f"(<code>{event.beneficiary_ticker}</code>, {event.beneficiary_tier}, 市值约 ${cap_b}B)<br>",
        f"<b>锚点</b>：{event.anchor_name} ({anchor_ticker_display}, {event.anchor_tier})<br>",
        f"<b>关系</b>：{event.tier_pair}<br><br>",
        f"<b>材料性</b>：{event.materiality_score}/100<br>",
        f"<b>关键词</b>：{kw_str}<br><br>",
        f"<b>标题</b>：{event.headline}<br>",
        f"<b>时间</b>：{event.published_at.strftime('%Y-%m-%d %H:%M UTC')}<br>",
        f"<b>来源</b>：<a href=\"{event.source_url}\">链接</a><br>",
    ]
    if event.tier_pair == "T0_T0":
        lines.append("<br>⚠️ 双巨头合作，小票弹性有限，请谨慎。<br>")
    lines.append(
        f"<br>---<br>⚠️ 非投资建议；7 日内 <code>{event.beneficiary_ticker}</code> 同类事件仅推一次。"
    )
    return title, "".join(lines)


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
) -> bool:
    """同一受益方+锚点 7 日内不重复入库（覆盖 compute_deal / ai_platform_deal）。"""
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
    return q.first() is not None


def _push_succeeded(event: DealEvent) -> bool:
    ch = (event.push_channel or "").strip()
    return bool(
        event.pushed_at
        and ch
        and ch not in {"none", "failed", "unconfigured", "disabled", "rate_limited"}
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


async def _maybe_push(db: Session, event: DealEvent, roles_should_push: bool) -> None:
    """仅在渠道真正发送成功时写入 pushed_at；失败可被后续重试。"""
    if not roles_should_push or not DEAL_PUSH_ENABLED:
        event.push_channel = "disabled"
        return
    if _push_rate_limited(db, event.beneficiary_ticker):
        logger.info("推送频率限制，跳过 %s", event.beneficiary_ticker)
        event.push_channel = "rate_limited"
        return

    anchor_display = event.anchor_ticker or "未上市"
    title, content = build_push_content(event, anchor_display)
    results = await notify(title, content)
    if not results:
        logger.warning("推送通道未配置（PUSHPLUS/SERVERCHAN 为空），跳过 %s", event.beneficiary_ticker)
        event.push_channel = "unconfigured"
        event.pushed_at = None
        return

    channels = []
    if results.get("pushplus"):
        channels.append("pushplus")
    if results.get("serverchan"):
        channels.append("serverchan")
    if channels:
        event.pushed_at = now_beijing()
        event.push_channel = "+".join(channels)
        logger.info("已推送 %s via %s", event.beneficiary_ticker, event.push_channel)
    else:
        logger.warning("推送全部失败 %s results=%s", event.beneficiary_ticker, results)
        event.push_channel = "failed"
        event.pushed_at = None


async def retry_unpushed_events(db: Session, limit: int = 20) -> int:
    """补推：从未成功推送的事件（含误写 pushed_at 但 channel=none）。"""
    if not DEAL_PUSH_ENABLED:
        return 0
    pending = (
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
        .order_by(DealEvent.id.asc())
        .limit(limit)
        .all()
    )
    n = 0
    for event in pending:
        # 同受益标的 24h 内已成功推过 → 不再补推转载稿，避免刷屏
        if _push_rate_limited(db, event.beneficiary_ticker):
            event.push_channel = "rate_limited"
            db.add(event)
            continue
        await _maybe_push(db, event, roles_should_push=True)
        if _push_succeeded(event):
            n += 1
        db.add(event)
    if pending:
        db.commit()
    return n


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


async def process_item(db: Session, item: RawItem, llm_decision: LlmDecision | None = None) -> dict:
    stats = {"skipped": True, "reason": ""}
    if is_test_source_url(item.source_url):
        stats["reason"] = "测试/占位链接，不入库"
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
    else:
        ok, matched = passes_keyword_filter(text, source=item.source)
        if not ok:
            stats["reason"] = "关键词/合作词未通过"
            return stats

    h_hash = headline_hash(item.headline)

    if llm_decision and llm_decision.anchor_name and llm_decision.beneficiary_name:
        entity_a = await resolve_entity(llm_decision.anchor_name, context=text)
        entity_b = await resolve_entity(llm_decision.beneficiary_name, context=text)
    elif item.source == "sec_8k":
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

    if _same_company(entity_a, entity_b):
        stats["reason"] = "双方为同一公司，不是合作事件"
        return stats

    if is_channel_partner_entity(entity_a, text) or is_channel_partner_entity(entity_b, text):
        stats["reason"] = "含渠道商/分销商，非合作方"
        return stats

    await enrich_entity_tiers(db, [entity_a, entity_b])

    roles = assign_roles(entity_a, entity_b)
    if not roles:
        stats["reason"] = "角色判定失败"
        return stats

    if not roles.should_push:
        stats["reason"] = roles.skip_reason or "规则不推送"
        return stats

    score = score_materiality(text, item.source, matched)
    if llm_decision and llm_decision.llm_score:
        # 不把营销稿的 LLM 高分抬上去；材料性仍以规则分为准，LLM 只作下限闸
        if llm_decision.llm_score < 70:
            stats["reason"] = f"LLM 材料性不足 {llm_decision.llm_score} < 70"
            return stats
        # 企业 AI 平台合作：规则分可能偏低，允许 LLM 分数更多参与
        if (llm_decision.event_type or "") == "ai_platform_deal":
            score = min(100, max(score, llm_decision.llm_score))
        else:
            score = min(100, max(score, min(llm_decision.llm_score, score + 10)))
    threshold = score_threshold(roles.tier_pair)
    if score < threshold:
        stats["reason"] = f"材料性 {score} < {threshold}"
        return stats

    is_update = is_update_headline(item.headline)
    beneficiaries = [roles.beneficiary]
    if roles.push_both:
        cap_a = entity_a.market_cap_usd or 0
        cap_b = entity_b.market_cap_usd or 0
        if cap_a <= cap_b:
            beneficiaries = [e for e in (entity_a, entity_b) if e.ticker]
        else:
            beneficiaries = [e for e in (entity_b, entity_a) if e.ticker]

    event_type = (
        (llm_decision.event_type if llm_decision else None) or EVENT_TYPE
    )
    anchor_key = _anchor_dedup_key(roles.anchor.ticker, roles.anchor.name)
    saved = []
    for beneficiary in beneficiaries:
        ticker = beneficiary.ticker
        if not ticker:
            continue
        ticker = ticker.upper()
        if _is_duplicate(db, item.source_url, h_hash, ticker):
            stats["reason"] = "URL/标题去重"
            continue
        if _dedup_blocked(db, ticker, is_update, anchor_key=anchor_key):
            logger.info("7 天去重跳过 %s (anchor=%s)", ticker, anchor_key)
            continue

        event = _save_event(
            db, item, roles, score, matched, is_update, beneficiary, event_type=event_type
        )
        await _maybe_push(db, event, roles.should_push)
        saved.append(event.beneficiary_ticker)

    if not saved:
        stats["reason"] = roles.skip_reason or "去重或未推送"
        return stats

    db.commit()
    stats["skipped"] = False
    stats["saved"] = saved
    stats["score"] = score
    stats["tier_pair"] = roles.tier_pair
    return stats


async def run_pipeline() -> dict:
    """执行一轮 RSS / Finnhub / Google News / SEC 抓取与处理。"""
    registry.load_seed()
    pr_items = await fetch_pr_wires()
    agg_items = await fetch_company_ir_and_aggregators()
    sec_items = await fetch_sec_8k()
    # URL 去重：同一通稿可能同时出现在 PRN / Finnhub / Google
    items: list[RawItem] = []
    seen_fetch: set[str] = set()
    for batch in (pr_items, agg_items, sec_items):
        for item in batch:
            url = (item.source_url or "").strip()
            if not url or url in seen_fetch:
                continue
            seen_fetch.add(url)
            items.append(item)
    summary = {
        "fetched": len(items),
        "fetched_pr": len(pr_items),
        "fetched_agg": len(agg_items),
        "fetched_sec_8k": len(sec_items),
        "fetched_new": 0,
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
