"""入库策略：LLM 主导 + 规则硬否决。

设计目标（避免事后堆关键词补洞）：
- 进 LLM 前只丢掉人一眼会扔的垃圾（股价反应稿 / SEO / 黑名单）
- 是否 AI 产业链合作、材料性几分 → 以 LLM 为准
- 规则质量档只约束软整合/融资/并购的封顶与是否推送，不否决 LLM 高分硬单
"""

from __future__ import annotations

from app.deal_monitor.config import DEAL_LLM_MIN_SCORE, DEAL_LLM_PRIMARY, DEAL_USE_LLM
from app.deal_monitor.content_filter import hard_reject_deal_item, reject_deal_item
from app.deal_monitor.fetchers.pr_wire import RawItem
from app.deal_monitor.llm_classifier import LlmDecision
from app.deal_monitor.materiality import (
    QUALITY_FINANCING,
    QUALITY_MA,
    QUALITY_SOFT_PRODUCT,
    QUALITY_VAGUE,
    classify_deal_quality,
    finalize_materiality_score,
)


def llm_primary_enabled() -> bool:
    return bool(DEAL_USE_LLM and DEAL_LLM_PRIMARY)


def pre_llm_reject(item: RawItem) -> tuple[bool, str]:
    """送 LLM 前的闸门：LLM 主导时仅硬否决。"""
    if llm_primary_enabled():
        return hard_reject_deal_item(item)
    return reject_deal_item(item)


def llm_ingest_score(
    text: str,
    source: str,
    decision: LlmDecision,
    *,
    matched: list[str] | None = None,
) -> int:
    """LLM 主导下的材料性分：以 llm_score 为主，规则只做弱档封顶。"""
    return finalize_materiality_score(
        text,
        source,
        matched or ["LLM"],
        llm_score=decision.llm_score,
        event_type=decision.event_type,
        llm_primary=llm_primary_enabled(),
    )


def should_skip_vague_despite_llm(text: str, decision: LlmDecision) -> bool:
    """空话战略合作：LLM 也给不出高分时才丢；高分则信任模型。"""
    if classify_deal_quality(text) != QUALITY_VAGUE:
        return False
    if not llm_primary_enabled():
        return True
    return int(decision.llm_score or 0) < DEAL_LLM_MIN_SCORE


def llm_allows_beneficiary_ingest(decision: LlmDecision, *, has_ticker: bool) -> bool:
    """有 ticker 且 LLM 过最低分 → 允许入库（即使档位默认不推）。"""
    if not has_ticker:
        return False
    if not decision.is_relevant:
        return False
    return int(decision.llm_score or 0) >= DEAL_LLM_MIN_SCORE


def soft_quality_push_cap(quality: str) -> bool:
    """弱催化入库可对照，默认不推。"""
    return quality in {
        QUALITY_SOFT_PRODUCT,
        QUALITY_FINANCING,
        QUALITY_VAGUE,
        QUALITY_MA,
    }
