"""deal_monitor 内容过滤单测。"""

from datetime import datetime, timezone

from types import SimpleNamespace

from app.deal_monitor.content_filter import (
    deal_amount_keys,
    hard_reject_deal_item,
    is_fresh_deal_announcement,
    is_material_signed_deal,
    is_price_reaction_rehash,
    is_seo_spam_headline,
    is_trusted_news_source,
    reject_deal_item,
    should_hide_deal_content,
    should_hide_deal_event,
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


def test_equinix_shares_rise_rejected():
    h = "Equinix Shares Rise 2% After Launch of AI Inference Program With Nvidia and Together AI"
    assert is_price_reaction_rehash(h)
    reject, reason = reject_deal_item(_item(h, source="finnhub:EQIX"))
    assert reject is True
    assert "股价" in reason or "旧闻" in reason


def test_rum_anthropic_price_headline_salvaged():
    """MarketWatch 式股价标题 + Anthropic×RUM 大额算力正文：应放行。"""
    from app.deal_monitor.content_filter import is_salvageable_ai_compute_deal

    h = "Rum Group Shares Gain on Report of $13.7 Billion Computing Deal With Anthropic"
    s = (
        "Shares of Rum Group rose after The Information reported a computing deal "
        "with Anthropic worth $13.7 billion. Anthropic would lease GPU compute capacity "
        "at a Maysville, Georgia data center under a six-year agreement."
    )
    assert is_price_reaction_rehash(h)
    assert is_salvageable_ai_compute_deal(f"{h}\n{s}")
    reject, reason = hard_reject_deal_item(
        _item(h, source="google_news:MarketWatch", summary=s)
    )
    assert reject is False, reason
    reject2, _ = reject_deal_item(_item(h, source="google_news:MarketWatch", summary=s))
    assert reject2 is False


def test_fervo_jumps_rejected_even_with_signs_deal():
    h = "Fervo Energy Stock Jumps 24% After Google Signs Utah Geothermal Power Deal"
    reject, reason = reject_deal_item(_item(h, source="finnhub:GOOGL"))
    assert reject is True, reason


def test_gorilla_surges_rejected():
    h = "Gorilla Surges 5% as AI Infrastructure Spending Ramps: Is a B- Credit Rating Enough to Fund It?"
    reject, reason = reject_deal_item(_item(h, source="finnhub:PLTR"))
    assert reject is True, reason


def test_nebius_commentary_rejected():
    h = "How Nebius Group’s AI Data Center Power Surge Could Reshape Nebius Group (NBIS) Investors"
    # LLM 主导：硬否决不拦评论；旧模式仍拦
    assert hard_reject_deal_item(_item(h, source="finnhub:NBIS"))[0] is False
    reject, reason = reject_deal_item(_item(h, source="finnhub:NBIS"), llm_primary=False)
    assert reject is True, reason


def test_weak_mining_cease_rejected():
    h = (
        "Hyperscale Data Has Ceased Bitcoin Mining Operations in Michigan as It Fulfills "
        "the Requirements of the AI Dat"
    )
    assert hard_reject_deal_item(_item(h, source="pr_newswire"))[0] is False
    reject, reason = reject_deal_item(_item(h, source="pr_newswire"), llm_primary=False)
    assert reject is True, reason


def test_stocktwits_blocked():
    h = "OpenAI And Visa Partner To Let AI Agents Shop And Pay For You - Stocktwits"
    reject, reason = reject_deal_item(_item(h, source="google_news:Stocktwits"))
    assert reject is True, reason


def test_amount_keys_for_story_dedup():
    assert "35b" in deal_amount_keys("Anthropic signs $35 billion cloud deal")
    assert deal_amount_keys("Anthropic signs $35B Cloud Deal") & deal_amount_keys(
        "Nvidia Circular AI: Anthropic Signs $35B Cloud Deal Using Hut 8 Campus"
    )
    assert "396mw" in deal_amount_keys(
        "Fervo Energy and Google Sign 396 MW PPA for Cape Station"
    )
    assert "396mw" in deal_amount_keys("签署约396兆瓦购电协议")


def test_should_hide_reddit_mshale_from_db_fields():
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


def _ns(**kwargs):
    base = dict(
        summary="",
        source="finnhub:LITE",
        source_url="https://example.com/1",
        published_at=datetime.now(timezone.utc),
        first_day_band="高",
        first_day_return=0.12,
        first_day_anomaly=False,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_hard_hide_ignores_first_day_rally():
    """股价反应/周报：即使首日大涨也不展示。"""
    assert should_hide_deal_event(
        _ns(headline="谷歌地热供电（标题已写大涨）")
    )
    assert should_hide_deal_event(
        _ns(headline="同上周报光学叙事", source="finnhub:MRVL")
    )
    assert should_hide_deal_event(
        _ns(headline="同主题 Anthropic 云协议续闻", source="google_news:Seeking Alpha")
    )


def test_agg_non_fresh_hidden_even_if_anomaly():
    """聚合源非新签：与入库同标准，异常涨跌也不展示。"""
    assert should_hide_deal_event(
        _ns(
            headline="MRVL：数据中心业务解读稿",
            source="finnhub:MRVL",
            first_day_anomaly=True,
            first_day_band="中",
        )
    )


def test_reuters_signed_deal_wire_still_shown():
    """路透新签金额快讯：虽是聚合源，但像一手通告，可留。"""
    ev = _ns(
        headline="Anthropic signs $35 billion cloud deal with Nvidia-backed Lambda, source says",
        source="google_news:Reuters",
        summary=(
            "Anthropic has signed a cloud-computing deal worth $35 billion with Lambda. "
            "The project is being developed by Hut 8 in Nueces County, Texas."
        ),
    )
    assert should_hide_deal_event(ev) is False


def test_cfo_talk_agg_rejected():
    """CFO 表态类二手简讯：旧模式聚合源且非新签 → 拒；LLM 主导交给模型。"""
    item = _item(
        "Nvidia CFO talks AWS GPU ramp at Amazon",
        source="google_news:Reuters",
        summary="Nvidia's CFO discussed cloud GPU demand.",
    )
    assert hard_reject_deal_item(item)[0] is False
    reject, reason = reject_deal_item(item, llm_primary=False)
    assert reject is True
    assert "非新签" in reason


def test_fervo_google_ppa_primary_allowed():
    """Fervo×Google 地热 PPA 一手通稿应放行（非股价反应稿）。"""
    headline = "Fervo Energy and Google Sign 396 MW PPA"
    summary = (
        "HOUSTON, Sept. 01, 2026 (GLOBE NEWSWIRE) -- Fervo Energy (Nasdaq: FRVO) today announced "
        "a 396-megawatt power purchase agreement with Google for Cape Station geothermal, "
        "expected to serve a potential data center in Utah."
    )
    item = _item(headline, source="google_news:GlobeNewswire", summary=summary)
    reject, reason = reject_deal_item(item)
    assert reject is False, reason
    assert is_fresh_deal_announcement(f"{headline}\n{summary}")


def test_fervo_shares_jump_still_rejected():
    h = "Fervo Energy Stock Jumps 24% After Google Signs Utah Geothermal Power Deal"
    reject, reason = reject_deal_item(_item(h, source="finnhub:GOOGL"))
    assert reject is True
    assert "股价" in reason or "旧闻" in reason


def test_mining_pivot_headline_hidden():
    event = SimpleNamespace(
        headline="GPUS：停挖矿转 AI（小票波动大）",
        summary="Master Services Agreement up to $3 Billion at Michigan Data Center",
        source="pr_newswire",
        source_url="https://www.prnewswire.com/news-releases/example",
        published_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    assert should_hide_deal_event(event) is True


def test_rum_group_entity_extractable():
    from app.deal_monitor.entities import registry

    registry._loaded = False
    registry._aliases = []
    registry.load_seed()
    ents = registry.extract_entities(
        "Anthropic signed a $13.7 billion compute deal with Rum Group"
    )
    assert any(e.ticker == "RUM" for e in ents)
    assert any(e.unlisted_id == "anthropic" for e in ents)


def test_marvell_tsm_status_quo_commentary_rejected():
    """既有代工关系解读：无新签，应硬拒（含 LLM 主导模式）。"""
    from app.deal_monitor.content_filter import is_status_quo_relationship_piece

    h = "Marvell Is Winning Custom AI Business. T"
    s = (
        "Marvell Technology is betting heavily on custom AI accelerators. "
        "Taiwan Semiconductor occupies a particularly powerful position in that strategy. "
        "Marvell's latest regulatory filing says TSM is currently the sole wafer supplier "
        "for its advanced-node products, including its 3nm products."
    )
    assert is_status_quo_relationship_piece(f"{h}\n{s}")
    reject, reason = hard_reject_deal_item(
        _item(h, source="finnhub:MRVL", summary=s)
    )
    assert reject is True
    assert "既有" in reason or "解读" in reason


def test_same_ticker_anchor_beneficiary_hidden():
    event = SimpleNamespace(
        headline="TSM：Marvell Is Winning Custom AI Business. T",
        summary="TSM occupies a powerful position as sole supplier.",
        source="finnhub:MRVL",
        source_url="https://finnhub.io/api/news?id=x",
        published_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        anchor_ticker="TSM",
        beneficiary_ticker="TSM",
        anchor_name="TSMC",
        beneficiary_name="TSMC",
    )
    assert should_hide_deal_event(event) is True


def test_assign_roles_rejects_same_company():
    from app.deal_monitor.entities import Entity
    from app.deal_monitor.tiers import assign_roles

    a = Entity(name="TSMC", ticker="TSM", tier="T0")
    b = Entity(name="Taiwan Semiconductor", ticker="TSM", tier="T0")
    assert assign_roles(a, b) is None


def test_real_ppa_not_status_quo():
    from app.deal_monitor.content_filter import is_status_quo_relationship_piece

    text = (
        "Fervo Energy today announced a 396-megawatt power purchase agreement with Google "
        "for Cape Station geothermal."
    )
    assert is_status_quo_relationship_piece(text) is False
    assert hard_reject_deal_item(
        _item("Fervo and Google Sign 396 MW PPA", source="globenewswire", summary=text)
    )[0] is False
