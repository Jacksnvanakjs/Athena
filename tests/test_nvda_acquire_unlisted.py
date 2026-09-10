"""NVDA 收购未上市巨头 → 黄仁勋板块（受益方 NVDA）。"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.deal_monitor.entities import Entity
from app.deal_monitor.fetchers.pr_wire import RawItem
from app.nvda_signal.classifier import classify_signal
from app.nvda_signal.keywords import detect_action_type, has_acquire_terms, _norm
from app.nvda_signal.pipeline import (
    _allow_t0_beneficiary,
    _is_nvda_acquire_unlisted,
    _nvda_as_beneficiary,
    _unlisted_target_in_headline,
    process_item,
)


def test_acquire_tokens_and_unlisted_detect():
    headline = "NVIDIA to Acquire Hugging Face"
    summary = "NVIDIA today announced a definitive agreement to acquire Hugging Face."
    assert _unlisted_target_in_headline(headline)
    assert _is_nvda_acquire_unlisted(headline, summary)
    assert has_acquire_terms(_norm(summary))
    assert detect_action_type(_norm(summary), "A") == "NVDA_ACQUIRE"


def test_unlisted_without_acquire_still_blocked_flag():
    headline = "Hugging Face expands NVIDIA NIM integration"
    assert _unlisted_target_in_headline(headline)
    assert not _is_nvda_acquire_unlisted(headline, headline)


def test_classify_acquire_as_a_tier():
    text = "NVIDIA agrees to acquire Hugging Face under a definitive agreement worth $12.9 billion."
    c = classify_signal(text, has_prior_a=False)
    assert c is not None
    assert c.signal_tier == "A"
    assert c.action_type == "NVDA_ACQUIRE"
    assert c.status == "confirmed"


def test_allow_t0_only_for_acquire_unlisted():
    nvda = _nvda_as_beneficiary()
    assert _allow_t0_beneficiary(nvda, "NVDA_ACQUIRE_UNLISTED")
    assert not _allow_t0_beneficiary(nvda, "NVDA_INVEST")
    assert not _allow_t0_beneficiary(
        Entity(name="Microsoft", ticker="MSFT", tier="T0"),
        "NVDA_ACQUIRE_UNLISTED",
    )


def test_process_item_saves_nvda_for_hf_acquire():
    import asyncio

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base, NvdaSignalEvent

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[NvdaSignalEvent.__table__])
    db = sessionmaker(bind=engine)()

    item = RawItem(
        headline="NVIDIA to Acquire Hugging Face",
        summary=(
            "NVIDIA and Hugging Face today announced a definitive agreement under which "
            "NVIDIA will acquire Hugging Face. The transaction values Hugging Face at "
            "approximately $12.9 billion."
        ),
        source="nvidia_newsroom",
        source_url="https://blogs.nvidia.com/blog/nvidia-to-acquire-hugging-face/",
        published_at=datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc),
    )

    async def _run():
        with (
            patch(
                "app.nvda_signal.pipeline.resolve_entity",
                new=AsyncMock(return_value=Entity(name="NVIDIA", ticker="NVDA", tier="T0")),
            ),
            patch("app.nvda_signal.pipeline.enrich_entity_tiers", new=AsyncMock()),
            patch("app.nvda_signal.pipeline._maybe_push", new=AsyncMock()),
            patch("app.nvda_signal.pipeline.find_prior_a", return_value=None),
        ):
            return await process_item(db, item)

    result = asyncio.run(_run())

    assert result.get("skipped") is False, result
    assert result.get("saved") == ["NVDA"]

    ev = (
        db.query(NvdaSignalEvent)
        .filter(NvdaSignalEvent.source_url == item.source_url)
        .one()
    )
    assert ev.beneficiary_ticker == "NVDA"
    assert ev.action_type == "NVDA_ACQUIRE_UNLISTED"
    assert ev.signal_tier == "A"
    assert ev.materiality_score >= 78
    assert ev.materiality_score <= 82
