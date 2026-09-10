"""LLM 主导入库回放：正负例固定，避免事后堆词补洞。

这些用例模拟「人一眼能判」的一手原文（非富途转载）。
不调用真实 Gemini，注入 LlmDecision，验证闸门与打分策略。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.deal_monitor.content_filter import hard_reject_deal_item, reject_deal_item
from app.deal_monitor.entities import Entity, registry
from app.deal_monitor.fetchers.pr_wire import RawItem
from app.deal_monitor.fetchers.prefilter import BROAD_DEAL_PREFILTER
from app.deal_monitor.ingest_policy import (
    llm_allows_beneficiary_ingest,
    llm_ingest_score,
    pre_llm_reject,
    should_skip_vague_despite_llm,
)
from app.deal_monitor.llm_classifier import LlmDecision
from app.deal_monitor.materiality import (
    QUALITY_HARD,
    classify_deal_quality,
    finalize_materiality_score,
)
from app.deal_monitor.pipeline import process_item
from app.deal_monitor.tiers import RoleAssignment


def _item(
    headline: str,
    summary: str,
    *,
    source: str = "ir:VZ",
    url: str = "https://example.com/deal",
) -> RawItem:
    return RawItem(
        headline=headline,
        summary=summary,
        source=source,
        source_url=url,
        published_at=datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc),
    )


CORNING = _item(
    "Verizon and Corning announce multi-year, multi-billion dollar supply agreement "
    "for broadband expansion and next-gen AI infrastructure",
    "Verizon and Corning have reached a multi-billion dollar supply agreement through 2032 "
    "for over 80 million miles of high-density optical fiber. The fiber will help build the "
    "long-haul connectivity backbone required by AI hyperscalers.",
    source="ir:VZ",
    url="https://www.verizon.com/about/news/verizon-corning-broadband-ai-fiber-agreement",
)

HUT_LAMBDA = _item(
    "Anthropic signs $35 billion cloud deal with Nvidia-backed Lambda",
    "Anthropic has signed a cloud-computing deal worth $35 billion with Lambda. "
    "The project is being developed in Nueces County by Hut 8.",
    source="google_news:Reuters",
    url="https://www.reuters.com/example-anthropic-lambda",
)

PRICE_MOVE = _item(
    "Corning shares jump 7% after multi-billion fiber deal with Verizon",
    "Shares of Corning surged after a supply agreement with Verizon for AI infrastructure.",
    source="google_news:CNBC",
    url="https://www.cnbc.com/glw-surge",
)

VAGUE = _item(
    "Company expands strategic partnership with OpenAI to explore collaboration",
    "The parties will explore opportunities to collaborate on AI initiatives.",
    source="pr_newswire",
    url="https://www.prnewswire.com/example-vague",
)

STOCKTWITS = _item(
    "OpenAI And Visa Partner To Let AI Agents Shop - Stocktwits",
    "Partnership announced on Stocktwits.",
    source="google_news:Stocktwits",
    url="https://stocktwits.com/x",
)

COMMENTARY = _item(
    "How Nebius Group’s AI Data Center Power Surge Could Reshape Investors",
    "Investors should watch power costs as Nebius expands data centers.",
    source="finnhub:NBIS",
    url="https://www.reuters.com/markets/nebius-commentary-example",
)


def test_prefilter_lets_corning_fiber_through():
    blob = f"{CORNING.headline}\n{CORNING.summary}"
    assert BROAD_DEAL_PREFILTER.search(blob)


def test_hard_reject_price_and_seo_only():
    assert hard_reject_deal_item(PRICE_MOVE)[0] is True
    assert hard_reject_deal_item(STOCKTWITS)[0] is True
    assert hard_reject_deal_item(COMMENTARY)[0] is False
    assert hard_reject_deal_item(CORNING)[0] is False


def test_pre_llm_reject_llm_primary_passes_commentary():
    reject, _ = pre_llm_reject(COMMENTARY)
    assert reject is False
    reject_legacy, reason = reject_deal_item(COMMENTARY, llm_primary=False)
    assert reject_legacy is True
    assert "评论" in reason or "展望" in reason


def test_corning_llm_score_not_crushed_by_rules():
    text = f"{CORNING.headline}\n{CORNING.summary}"
    assert classify_deal_quality(text) == QUALITY_HARD
    decision = LlmDecision(
        source_url=CORNING.source_url,
        is_relevant=True,
        anchor_name="Verizon",
        beneficiary_name="Corning",
        llm_score=85,
        reason="电信×光纤多年供应，服务 AI 骨干",
    )
    score = llm_ingest_score(text, CORNING.source, decision)
    assert score >= 85
    legacy = finalize_materiality_score(
        text, CORNING.source, ["LLM"], llm_score=85, llm_primary=False
    )
    primary = finalize_materiality_score(
        text, CORNING.source, ["LLM"], llm_score=85, llm_primary=True
    )
    # 主导模式至少保住 LLM 分
    assert primary >= 85


def test_vague_skipped_unless_llm_high():
    text = f"{VAGUE.headline}\n{VAGUE.summary}"
    low = LlmDecision(
        source_url=VAGUE.source_url,
        is_relevant=True,
        anchor_name="OpenAI",
        beneficiary_name="Company",
        llm_score=40,
        reason="explore only",
    )
    high = LlmDecision(
        source_url=VAGUE.source_url,
        is_relevant=True,
        anchor_name="OpenAI",
        beneficiary_name="Company",
        llm_score=82,
        reason="实质商业落地",
    )
    assert should_skip_vague_despite_llm(text, low) is True
    assert should_skip_vague_despite_llm(text, high) is False


def test_llm_allows_beneficiary():
    d = LlmDecision(
        source_url="x",
        is_relevant=True,
        beneficiary_name="Corning",
        llm_score=85,
    )
    assert llm_allows_beneficiary_ingest(d, has_ticker=True)
    assert not llm_allows_beneficiary_ingest(d, has_ticker=False)
    d.llm_score = 50
    assert not llm_allows_beneficiary_ingest(d, has_ticker=True)


def test_replay_corning_process_item_saves():
    registry.load_seed()
    decision = LlmDecision(
        source_url=CORNING.source_url,
        is_relevant=True,
        anchor_name="Verizon",
        beneficiary_name="Corning",
        llm_score=85,
        reason="实质：光纤多年供应服务 AI 骨干",
    )
    vz = Entity(name="Verizon", ticker="VZ", tier="T0")
    glw = Entity(name="Corning", ticker="GLW", tier="T1")
    roles = RoleAssignment(
        anchor=vz,
        beneficiary=glw,
        tier_pair="T0_T1",
        should_push=True,
        push_both=False,
    )
    db = MagicMock()

    async def run():
        with (
            patch(
                "app.deal_monitor.pipeline._resolve_llm_anchor_and_beneficiaries",
                new=AsyncMock(return_value=(vz, [glw], None)),
            ),
            patch("app.deal_monitor.pipeline.assign_roles", return_value=roles),
            patch("app.deal_monitor.pipeline._is_duplicate", return_value=False),
            patch("app.deal_monitor.pipeline._dedup_blocked", return_value=False),
            patch("app.deal_monitor.pipeline._save_event") as save_mock,
            patch("app.deal_monitor.pipeline._maybe_push", new=AsyncMock()),
            patch(
                "app.deal_monitor.pipeline._published_too_stale_for_push",
                return_value=False,
            ),
        ):
            event = MagicMock()
            event.beneficiary_ticker = "GLW"
            event.published_at = datetime(2026, 9, 8)
            save_mock.return_value = event
            return await process_item(db, CORNING, decision)

    result = asyncio.run(run())
    assert result.get("skipped") is False
    assert "GLW" in result.get("saved", [])
    assert result.get("score", 0) >= 85


def test_replay_price_move_hard_rejected():
    db = MagicMock()
    decision = LlmDecision(
        source_url=PRICE_MOVE.source_url,
        is_relevant=True,
        anchor_name="Verizon",
        beneficiary_name="Corning",
        llm_score=90,
        reason="would have been relevant if primary",
    )
    result = asyncio.run(process_item(db, PRICE_MOVE, decision))
    assert result["skipped"] is True
    assert "股价" in result["reason"] or "旧闻" in result["reason"]


def test_replay_llm_says_irrelevant():
    db = MagicMock()
    decision = LlmDecision(
        source_url=COMMENTARY.source_url,
        is_relevant=False,
        llm_score=20,
        reason="评论展望稿",
    )
    assert pre_llm_reject(COMMENTARY)[0] is False
    result = asyncio.run(process_item(db, COMMENTARY, decision))
    assert result["skipped"] is True
    assert "不相关" in result["reason"]


def test_replay_hut8_indirect_beneficiary():
    registry.load_seed()
    decision = LlmDecision(
        source_url=HUT_LAMBDA.source_url,
        is_relevant=True,
        anchor_name="NVIDIA",
        beneficiary_name="Hut 8",
        llm_score=88,
        reason="间接受益 DC 建设方",
    )
    nvda = Entity(name="NVIDIA", ticker="NVDA", tier="T0")
    hut = Entity(name="Hut 8", ticker="HUT", tier="T2")
    roles = RoleAssignment(
        anchor=nvda,
        beneficiary=hut,
        tier_pair="T0_T2",
        should_push=True,
        push_both=False,
    )
    db = MagicMock()

    async def run():
        with (
            patch(
                "app.deal_monitor.pipeline._resolve_llm_anchor_and_beneficiaries",
                new=AsyncMock(return_value=(nvda, [hut], None)),
            ),
            patch("app.deal_monitor.pipeline.assign_roles", return_value=roles),
            patch("app.deal_monitor.pipeline._is_duplicate", return_value=False),
            patch("app.deal_monitor.pipeline._dedup_blocked", return_value=False),
            patch("app.deal_monitor.pipeline._save_event") as save_mock,
            patch("app.deal_monitor.pipeline._maybe_push", new=AsyncMock()),
            patch(
                "app.deal_monitor.pipeline._published_too_stale_for_push",
                return_value=False,
            ),
        ):
            event = MagicMock()
            event.beneficiary_ticker = "HUT"
            event.published_at = datetime(2026, 9, 1)
            save_mock.return_value = event
            return await process_item(db, HUT_LAMBDA, decision)

    result = asyncio.run(run())
    assert result.get("skipped") is False
    assert "HUT" in result.get("saved", [])
