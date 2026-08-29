"""deal_monitor 内容过滤单测。"""

from app.deal_monitor.content_filter import (
    is_price_reaction_rehash,
    is_seo_spam_headline,
    reject_deal_item,
)
from app.deal_monitor.fetchers.pr_wire import RawItem


def _item(headline: str, source: str = "google_news:Mshale", summary: str = "") -> RawItem:
    return RawItem(
        headline=headline,
        summary=summary,
        source=source,
        source_url="https://news.google.com/rss/articles/example",
        published_at=None,
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


def test_price_reaction_without_fresh_cues():
    assert is_price_reaction_rehash("Reddit stock jumps 13% after OpenAI partnership")


def test_seo_spam_hash():
    assert is_seo_spam_headline("Some headline (Glbua6qxBn) - Mshale")
