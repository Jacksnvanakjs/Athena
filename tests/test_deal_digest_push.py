"""积压快讯合并推送文案。"""

from types import SimpleNamespace

from app.push_format import (
    build_deal_digest_push_content,
    build_deal_push_content,
    build_nvda_push_content,
)


def _evt(**kwargs):
    base = dict(
        beneficiary_ticker="SNOW",
        beneficiary_name="Snowflake",
        anchor_ticker="MSFT",
        anchor_name="Microsoft",
        headline="SNOW：与微软扩大云数据合作",
        summary="Microsoft expands Snowflake partnership for cloud data platform.",
        source_url="https://example.com/a",
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_digest_single_falls_back_to_normal():
    e = _evt()
    t1, c1 = build_deal_push_content(e)
    t2, c2 = build_deal_digest_push_content([e])
    assert t1 == t2
    assert c1 == c2


def test_digest_merges_multiple():
    events = [
        _evt(beneficiary_ticker="SNOW", headline="SNOW：合作 A"),
        _evt(beneficiary_ticker="DDOG", beneficiary_name="Datadog", headline="DDOG：合作 B"),
        _evt(beneficiary_ticker="SNOW", headline="SNOW：合作 A 再发"),  # same ticker ok in builder
    ]
    title, body = build_deal_digest_push_content(events)
    assert title.startswith("[AI合作·综合] 3条")
    assert "SNOW" in title and "DDOG" in title
    assert "积压合并推送" in body
    assert "合作 A" in body
    assert "合作 B" in body


def test_deal_push_uses_chinese_core_not_english_summary():
    e = _evt(
        headline="Microsoft expands Snowflake partnership",
        summary="Microsoft and Snowflake announced a multi-year cloud data deal worth billions.",
    )
    title, body = build_deal_push_content(e)
    assert "AI合作" in title
    assert "Microsoft expands" not in body
    assert "multi-year cloud data deal" not in body
    assert "原文：https://example.com/a" in body
    # 中文核心句（规则提炼）
    assert "：" in body or "合作" in body or "微软" in body or "云" in body


def test_nvda_push_uses_chinese_core_not_english_summary():
    e = SimpleNamespace(
        beneficiary_ticker="AVGO",
        beneficiary_name="Broadcom",
        signal_tier="A",
        buy_ok=True,
        buy_window="T+1",
        sell_window="T+5",
        headline="Nvidia and Broadcom deepen AI networking partnership",
        summary="Jensen Huang said Nvidia will work more closely with Broadcom on AI networking chips.",
        source_url="https://example.com/nvda",
    )
    title, body = build_nvda_push_content(e)
    assert title.startswith("[黄仁勋]")
    assert "Jensen Huang said" not in body
    assert "AI networking chips" not in body
    assert "买入窗口：T+1" in body
    assert "原文：https://example.com/nvda" in body
