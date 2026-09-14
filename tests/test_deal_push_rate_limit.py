"""推送限流：按正文内容指纹去重，小时上限 0=关闭。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.deal_monitor import pipeline as deal_pipe
from app.deal_monitor.story_fingerprint import is_same_story, story_fingerprint


def test_hour_limit_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(deal_pipe, "DEAL_MAX_PUSH_PER_HOUR", 0)
    monkeypatch.setattr(deal_pipe, "DEAL_MAX_PUSH_PER_BENEFICIARY_24H", 0)
    monkeypatch.setattr(deal_pipe, "DEAL_STORY_DEDUP_DAYS", 0)
    db = MagicMock()
    event = SimpleNamespace(
        beneficiary_ticker="SNOW",
        headline="SNOW：合作",
        summary="Microsoft expands Snowflake partnership",
        source_url="https://example.com/a",
        anchor_ticker="MSFT",
        anchor_name="Microsoft",
    )
    assert deal_pipe._push_rate_limited(db, event) is False
    db.query.assert_not_called()


def test_story_fingerprint_uses_body_not_just_title():
    a = story_fingerprint(
        "Title rewrite A",
        "Microsoft and Snowflake announced a multi-year cloud data platform deal worth billions.",
    )
    b = story_fingerprint(
        "Completely different headline B",
        "Microsoft and Snowflake announced a multi-year cloud data platform deal worth billions.",
    )
    # 同正文、不同标题 → 同指纹
    assert a == b
    c = story_fingerprint(
        "Title rewrite A",
        "Oracle signed a separate AI infrastructure contract with CoreWeave for GPU clusters.",
    )
    assert a != c


def test_is_same_story_prefix_truncation():
    full = (
        "Anthropic and Lambda signed a multi-billion cloud computing deal for training capacity. "
        * 3
    )
    assert is_same_story("H1", full, "H2", full[:180])


def test_same_body_blocks_push_different_body_allows(monkeypatch):
    monkeypatch.setattr(deal_pipe, "DEAL_MAX_PUSH_PER_HOUR", 0)
    monkeypatch.setattr(deal_pipe, "DEAL_MAX_PUSH_PER_BENEFICIARY_24H", 1)
    monkeypatch.setattr(deal_pipe, "DEAL_STORY_DEDUP_DAYS", 30)

    body = (
        "Microsoft and Snowflake announced a multi-year cloud data platform deal "
        "worth billions for enterprise AI workloads across Azure regions."
    )
    prior = SimpleNamespace(
        headline="旧标题",
        summary=body,
        source_url="https://example.com/old",
        anchor_ticker="MSFT",
        anchor_name="Microsoft",
        pushed_at=True,
    )
    incoming_same = SimpleNamespace(
        beneficiary_ticker="SNOW",
        headline="新标题改写",
        summary=body,
        source_url="https://example.com/new",
        anchor_ticker="MSFT",
        anchor_name="Microsoft",
    )
    incoming_diff = SimpleNamespace(
        beneficiary_ticker="SNOW",
        headline="另一条新闻",
        summary=(
            "Snowflake unveiled a brand-new iceberg table accelerator for open lakehouse "
            "analytics with no Microsoft involvement in this release."
        ),
        source_url="https://example.com/other",
        anchor_ticker="MSFT",
        anchor_name="Microsoft",
    )

    class _Q:
        def filter(self, *a, **k):
            return self

        def all(self):
            return [prior]

    db = MagicMock()
    db.query.return_value = _Q()
    assert deal_pipe._push_rate_limited(db, incoming_same) is True
    assert deal_pipe._push_rate_limited(db, incoming_diff) is False


def test_fervo_finnhub_rehash_related_to_ppa_wire():
    from app.deal_monitor.story_fingerprint import is_related_deal_story

    wire_h = "FRVO：与谷歌签署犹他州地热 PPA（约396MW）"
    wire_s = (
        "HOUSTON, Sept. 01, 2026 (GLOBE NEWSWIRE) -- Fervo Energy (Nasdaq: FRVO) today "
        "announced a 396-megawatt (MW) power purchase agreement (PPA) with Google."
    )
    fh_h = "FRVO：与谷歌电力/购电合作"
    fh_s = (
        "Recent IPO stock Fervo Energy is building on its partnership with Google "
        "with a massive power purchase agreement."
    )
    assert is_related_deal_story(
        fh_h, fh_s, wire_h, wire_s, same_anchor=True
    )
    assert not is_related_deal_story(
        fh_h, fh_s, wire_h, wire_s, same_anchor=False
    )


def test_dedup_blocks_fervo_rehash_outside_7d(monkeypatch):
    """Finnhub 滞后稿：同受益方+同锚点+购电线索，即使超过 7 天 fetched 窗也拦。"""
    from datetime import datetime, timedelta

    monkeypatch.setattr(deal_pipe, "DEAL_DEDUP_DAYS", 7)
    monkeypatch.setattr(deal_pipe, "DEAL_STORY_DEDUP_DAYS", 30)
    monkeypatch.setattr(
        deal_pipe,
        "now_beijing",
        lambda: datetime(2026, 9, 13, 15, 21, 0),
    )

    prior = SimpleNamespace(
        headline="FRVO：与谷歌签署犹他州地热 PPA（约396MW）",
        summary=(
            "Fervo Energy today announced a 396-megawatt power purchase agreement "
            "with Google for Cape Station geothermal."
        ),
        source_url="https://www.globenewswire.com/example",
        anchor_ticker="GOOG",
        anchor_name="Google",
        fetched_at=datetime(2026, 9, 2, 3, 22, 0),
    )

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *a, **k):
            return self

        def first(self):
            # 硬窗 7 天：prior.fetched 在 9/2，相对 9/13 超窗 → 模拟无命中
            return None

        def all(self):
            return self._rows

    db = MagicMock()
    # 第一次 query（硬窗）→ first None；第二次（故事窗）→ all [prior]
    db.query.side_effect = [_Q([]), _Q([prior])]
    assert (
        deal_pipe._dedup_blocked(
            db,
            "FRVO",
            False,
            anchor_key="GOOG",
            headline="Fervo building on Google partnership",
            summary=(
                "Recent IPO stock Fervo Energy is building on its partnership with "
                "Google with a massive power purchase agreement."
            ),
        )
        is True
    )
