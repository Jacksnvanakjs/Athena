"""deal_monitor 内容过滤单测。"""

from datetime import datetime, timezone

from app.deal_monitor.content_filter import (
    is_material_signed_deal,
    is_price_reaction_rehash,
    is_seo_spam_headline,
    is_trusted_news_source,
    reject_deal_item,
)
from app.deal_monitor.fetchers.pr_wire import RawItem


def _item(headline: str, source: str = "google_news:Mshale", summary: str = "") -> RawItem:
    return RawItem(
        headline=headline,
        summary=summary,
        source=source,
        source_url="https://news.google.com/rss/articles/example",
        published_at=datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc),
    )


def test_reddit_openai_mshale_rejected():
    headline = (
        "Reddit Shares Up 11% After Announcing A Partnership With OpenAI "
        "Below Deck Mediterranean Season 11 (Glbua6qxBn) - Mshale"
    )
    item = _item(headline)
    reject, reason = reject_deal_item(item)
    assert reject is True
    assert reason


def test_fresh_partnership_google_news_allowed():
    headline = "CoreWeave enters into multi-year capacity agreement with Microsoft for AI compute"
    item = _item(
        headline,
        source="google_news:Reuters",
        summary="The companies announced a definitive agreement today.",
    )
    reject, _ = reject_deal_item(item)
    assert reject is False


def test_anthropic_lambda_reuters_allowed():
    headline = (
        "Anthropic signs $35 billion cloud deal with Nvidia-backed Lambda, source says"
    )
    summary = (
        "Anthropic has signed a cloud-computing deal worth $35 billion with Lambda. "
        "The project is being developed by Hut 8 in Nueces County, Texas."
    )
    item = _item(headline, source="google_news:Reuters", summary=summary)
    assert is_trusted_news_source(item)
    assert is_material_signed_deal(f"{headline}\n{summary}")
    reject, reason = reject_deal_item(item)
    assert reject is False, reason


def test_price_reaction_without_fresh_cues():
    assert is_price_reaction_rehash("Reddit stock jumps 13% after OpenAI partnership")


def test_should_hide_reddit_mshale_from_db_fields():
    from app.deal_monitor.content_filter import should_hide_deal_content

    headline = (
        "Reddit Shares Up 11% After Announcing A Partnership With OpenAI "
        "Below Deck Mediterranean Season 11 (Glbua6qxBn) - Mshale"
    )
    assert should_hide_deal_content(
        headline,
        summary="",
        source="google_news:Mshale",
        source_url="https://news.google.com/rss/articles/example",
    )


def test_crowdstrike_google_ir_passes_filter():
    from app.deal_monitor.keywords import is_product_only_integration

    headline = "CrowdStrike and Google Announce the Falcon Platform on Google Cloud"
    summary = (
        "August 31, 2026 – CrowdStrike (NASDAQ: CRWD) today announced the Falcon platform "
        "is now available on Google Cloud infrastructure, giving customers access to its "
        "leading AI-native security platform."
    )
    text = f"{headline}\n{summary}"
    item = _item(headline, source="ir:CRWD", summary=summary)
    reject, reason = reject_deal_item(item)
    assert reject is False, reason
    assert is_product_only_integration(text) is False
