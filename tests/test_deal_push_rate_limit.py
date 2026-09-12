"""推送限流：按正文内容指纹去重，小时上限 0=关闭。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.deal_monitor import pipeline as deal_pipe
from app.deal_monitor.story_fingerprint import is_same_story, story_fingerprint


def test_hour_limit_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(deal_pipe, "DEAL_MAX_PUSH_PER_HOUR", 0)
    monkeypatch.setattr(deal_pipe, "DEAL_MAX_PUSH_PER_BENEFICIARY_24H", 0)
    db = MagicMock()
    event = SimpleNamespace(
        beneficiary_ticker="SNOW",
        headline="SNOW：合作",
        summary="Microsoft expands Snowflake partnership",
        source_url="https://example.com/a",
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

    body = (
        "Microsoft and Snowflake announced a multi-year cloud data platform deal "
        "worth billions for enterprise AI workloads across Azure regions."
    )
    prior = SimpleNamespace(
        headline="旧标题",
        summary=body,
        source_url="https://example.com/old",
    )
    incoming_same = SimpleNamespace(
        beneficiary_ticker="SNOW",
        headline="新标题改写",
        summary=body,
        source_url="https://example.com/new",
    )
    incoming_diff = SimpleNamespace(
        beneficiary_ticker="SNOW",
        headline="另一条新闻",
        summary=(
            "Snowflake unveiled a brand-new iceberg table accelerator for open lakehouse "
            "analytics with no Microsoft involvement in this release."
        ),
        source_url="https://example.com/other",
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
