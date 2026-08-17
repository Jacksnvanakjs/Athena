"""抓取 → 解析 → 打分 → 去重 → 入库 → 推送。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import timedelta

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.database import DealEvent, db_session
from app.deal_monitor.config import (
    DEAL_DEDUP_DAYS,
    DEAL_MAX_PUSH_PER_BENEFICIARY_24H,
    DEAL_MAX_PUSH_PER_HOUR,
    DEAL_PUSH_ENABLED,
)
from app.deal_monitor.entities import Entity, registry
from app.deal_monitor.fetchers.pr_wire import RawItem, fetch_pr_wires
from app.deal_monitor.keywords import is_update_headline, passes_keyword_filter
from app.deal_monitor.market_cap import enrich_entity_tiers
from app.deal_monitor.materiality import score_materiality
from app.deal_monitor.parser import infer_partnership_pair
from app.deal_monitor.tiers import assign_roles, score_threshold
from app.notifier import notify
from app.utils import now_beijing

logger = logging.getLogger(__name__)

EVENT_TYPE = "compute_deal"


def normalize_headline(headline: str) -> str:
    text = headline.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text)


def headline_hash(headline: str) -> str:
    return hashlib.md5(normalize_headline(headline).encode()).hexdigest()


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


def _dedup_blocked(
    db: Session,
    beneficiary_ticker: str,
    is_update: bool,
) -> bool:
    if is_update:
        return False
    since = now_beijing() - timedelta(days=DEAL_DEDUP_DAYS)
    existing = (
        db.query(DealEvent)
        .filter(
            DealEvent.beneficiary_ticker == beneficiary_ticker,
            DealEvent.event_type == EVENT_TYPE,
            DealEvent.published_at >= since,
            DealEvent.is_update.is_(False),
        )
        .first()
    )
    return existing is not None


def _push_rate_limited(db: Session, beneficiary_ticker: str) -> bool:
    since_24h = now_beijing() - timedelta(hours=24)
    count_24h = (
        db.query(DealEvent)
        .filter(
            DealEvent.beneficiary_ticker == beneficiary_ticker,
            DealEvent.pushed_at.isnot(None),
            DealEvent.pushed_at >= since_24h,
        )
        .count()
    )
    if count_24h >= DEAL_MAX_PUSH_PER_BENEFICIARY_24H:
        return True

    since_1h = now_beijing() - timedelta(hours=1)
    global_count = (
        db.query(DealEvent)
        .filter(DealEvent.pushed_at.isnot(None), DealEvent.pushed_at >= since_1h)
        .count()
    )
    return global_count >= DEAL_MAX_PUSH_PER_HOUR


async def _maybe_push(db: Session, event: DealEvent, roles_should_push: bool) -> None:
    if not roles_should_push or not DEAL_PUSH_ENABLED:
        event.push_channel = "none"
        return
    if _push_rate_limited(db, event.beneficiary_ticker):
        logger.info("推送频率限制，跳过 %s", event.beneficiary_ticker)
        event.push_channel = "none"
        return

    anchor_display = event.anchor_ticker or "未上市"
    title, content = build_push_content(event, anchor_display)
    results = await notify(title, content)
    event.pushed_at = now_beijing()
    channels = []
    if results.get("pushplus"):
        channels.append("pushplus")
    if results.get("serverchan"):
        channels.append("serverchan")
    event.push_channel = "+".join(channels) if channels else "none"


def _save_event(
    db: Session,
    item: RawItem,
    roles,
    score: int,
    matched_keywords: list[str],
    is_update: bool,
    beneficiary: Entity,
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
        anchor_ticker=roles.anchor.ticker,
        anchor_tier=roles.anchor.tier,
        beneficiary_ticker=beneficiary.ticker.upper(),
        beneficiary_name=beneficiary.name,
        beneficiary_tier=beneficiary.tier,
        beneficiary_market_cap_usd=beneficiary.market_cap_usd,
        tier_pair=roles.tier_pair,
        materiality_score=score,
        matched_keywords=json.dumps(matched_keywords, ensure_ascii=False),
        event_type=EVENT_TYPE,
        is_update=is_update,
    )
    db.add(event)
    return event


async def process_item(db: Session, item: RawItem) -> dict:
    stats = {"skipped": True, "reason": ""}
    text = f"{item.headline}\n{item.summary}"
    ok, matched = passes_keyword_filter(text)
    if not ok:
        stats["reason"] = "关键词/合作词未通过"
        return stats

    h_hash = headline_hash(item.headline)

    entities = registry.extract_entities(text)
    if len(entities) < 2:
        stats["reason"] = "未识别到合作双方"
        return stats

    pair = infer_partnership_pair(item.headline, item.summary, entities)
    if not pair:
        stats["reason"] = "无法推断合作对"
        return stats

    entity_a, entity_b = pair
    await enrich_entity_tiers(db, [entity_a, entity_b])

    roles = assign_roles(entity_a, entity_b)
    if not roles:
        stats["reason"] = "角色判定失败"
        return stats

    if not roles.should_push:
        stats["reason"] = roles.skip_reason or "规则不推送"
        return stats

    score = score_materiality(text, item.source, matched)
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

    saved = []
    for beneficiary in beneficiaries:
        ticker = beneficiary.ticker
        if not ticker:
            continue
        ticker = ticker.upper()
        if _is_duplicate(db, item.source_url, h_hash, ticker):
            stats["reason"] = "URL/标题去重"
            continue
        if _dedup_blocked(db, ticker, is_update):
            logger.info("7 天去重跳过 %s", ticker)
            continue

        event = _save_event(db, item, roles, score, matched, is_update, beneficiary)
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
    """执行一轮 PR RSS 抓取与处理。"""
    registry.load_seed()
    items = await fetch_pr_wires()
    summary = {"fetched": len(items), "processed": 0, "saved": 0, "pushed": 0, "errors": []}

    with db_session() as db:
        registry.sync_to_db(db)
        for item in items:
            try:
                result = await process_item(db, item)
                if not result.get("skipped"):
                    summary["processed"] += 1
                    summary["saved"] += len(result.get("saved", []))
            except Exception as exc:
                logger.exception("处理条目失败: %s", item.headline[:80])
                summary["errors"].append(str(exc)[:200])
                db.rollback()

        pushed = (
            db.query(DealEvent)
            .filter(DealEvent.pushed_at.isnot(None))
            .order_by(desc(DealEvent.pushed_at))
            .limit(summary["saved"])
            .all()
        )
        summary["pushed"] = sum(1 for e in pushed if e.push_channel and e.push_channel != "none")

    logger.info("deal_monitor pipeline: %s", summary)
    return summary
