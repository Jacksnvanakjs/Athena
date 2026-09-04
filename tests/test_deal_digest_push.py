"""积压快讯合并推送文案。"""

from types import SimpleNamespace

from app.push_format import build_deal_digest_push_content, build_deal_push_content


def _evt(**kwargs):
    base = dict(
        beneficiary_ticker="SNOW",
        beneficiary_name="Snowflake",
        anchor_ticker="MSFT",
        anchor_name="Microsoft",
        headline="Microsoft expands Snowflake partnership",
        summary="Cloud data deal",
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
        _evt(beneficiary_ticker="SNOW", headline="Deal A"),
        _evt(beneficiary_ticker="DDOG", beneficiary_name="Datadog", headline="Deal B"),
        _evt(beneficiary_ticker="SNOW", headline="Deal A reprint"),  # same ticker ok in builder
    ]
    title, body = build_deal_digest_push_content(events)
    assert title.startswith("[AI合作·综合] 3条")
    assert "SNOW" in title and "DDOG" in title
    assert "积压合并推送" in body
    assert "Deal A" in body
    assert "Deal B" in body
