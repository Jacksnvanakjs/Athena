"""LLM 间接受益方解析单测。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.deal_monitor.llm_classifier import LlmDecision
from app.deal_monitor.pipeline import _resolve_llm_anchor_and_beneficiaries
from app.deal_monitor.entities import registry


def test_llm_decision_all_beneficiary_names():
    d = LlmDecision(
        source_url="x",
        is_relevant=True,
        beneficiary_name="Hut 8",
        beneficiary_names=["CoreWeave"],
    )
    assert d.all_beneficiary_names() == ["Hut 8", "CoreWeave"]


def test_resolve_hut8_from_anthropic_lambda_story():
    registry.load_seed()
    headline = "Anthropic signs $35 billion cloud deal with Nvidia-backed Lambda"
    summary = (
        "Anthropic has signed a cloud-computing deal worth $35 billion with Lambda. "
        "The project is being developed in Nueces County by Hut 8."
    )
    text = f"{headline}\n{summary}"
    decision = LlmDecision(
        source_url="https://example.com",
        is_relevant=True,
        anchor_name="NVIDIA",
        beneficiary_name="Hut 8",
        llm_score=88,
        reason="DC builder indirect beneficiary",
    )
    db = MagicMock()

    async def run():
        with patch(
            "app.deal_monitor.pipeline.enrich_entity_tiers",
            new_callable=AsyncMock,
        ):
            return await _resolve_llm_anchor_and_beneficiaries(db, decision, text)

    anchor, beneficiaries, err = asyncio.run(run())
    assert err is None
    assert anchor.ticker == "NVDA"
    assert len(beneficiaries) == 1
    assert beneficiaries[0].ticker == "HUT"


def test_resolve_crwd_from_google_cloud_story():
    registry.load_seed()
    headline = "CrowdStrike and Google Announce the Falcon Platform on Google Cloud"
    summary = (
        "CrowdStrike today announced the Falcon platform is now available on "
        "Google Cloud infrastructure."
    )
    text = f"{headline}\n{summary}"
    decision = LlmDecision(
        source_url="https://example.com/crwd",
        is_relevant=True,
        anchor_name="Google",
        beneficiary_name="CrowdStrike",
        event_type="ai_platform_deal",
        llm_score=78,
        reason="Falcon on GCP",
    )
    db = MagicMock()

    async def run():
        with patch(
            "app.deal_monitor.pipeline.enrich_entity_tiers",
            new_callable=AsyncMock,
        ):
            return await _resolve_llm_anchor_and_beneficiaries(db, decision, text)

    anchor, beneficiaries, err = asyncio.run(run())
    assert err is None
    assert anchor.ticker == "GOOG"
    assert len(beneficiaries) == 1
    assert beneficiaries[0].ticker == "CRWD"
