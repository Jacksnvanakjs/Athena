"""用库内风格的「首日正收益」样本回放 LLM 主导打分（不调在线 API）。

重点：旧 finalize 会把部分已验证正收益稿压到门槛下；主导模式应保住 LLM 分。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.deal_monitor.content_filter import hard_reject_deal_item
from app.deal_monitor.fetchers.pr_wire import RawItem
from app.deal_monitor.materiality import finalize_materiality_score


def _item(headline: str, summary: str, source: str) -> RawItem:
    return RawItem(
        headline=headline,
        summary=summary,
        source=source,
        source_url="https://www.reuters.com/backtest-replay/1",
        published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


# 真实库内正收益风格（摘要取自入库原文片段）
POSITIVE_SAMPLES = [
    (
        "SNOW",
        16.6,
        72,
        _item(
            "SNOW：第三方在 Snowflake 重建图谱",
            "The rebuild makes readable what earlier technology could not capture and is expected "
            "to cut data infrastructure costs. Sayari rebuilt its graph on Snowflake for AI.",
            "pr_newswire",
        ),
    ),
    (
        "ESTC",
        7.3,
        74,
        _item(
            "ESTC：接入 OpenAI 安全/业务能力",
            "Elastic today announced plans to bring OpenAI GPT cyber models into Elastic Security "
            "workflows by integrating these models directly.",
            "finnhub:MSFT",
        ),
    ),
    (
        "NOW",
        6.5,
        72,
        _item(
            "NOW：企业 AI 商务默认平台主题稿",
            "Pricefx announced its Deal Optimization App for the ServiceNow AI Platform, embedding "
            "AI-guided pricing into sales workflows.",
            "finnhub:NOW",
        ),
    ),
    (
        "FRVO",
        28.4,
        78,
        _item(
            "FRVO：与谷歌签署犹他州地热 PPA（约396MW）",
            "Fervo Energy today announced a 396-megawatt power purchase agreement with Google "
            "for Cape Station geothermal expected to serve a potential data center.",
            "globenewswire",
        ),
    ),
    (
        "GLW",
        7.6,
        85,
        _item(
            "GLW：与Verizon×康宁签光纤供应协议（数十亿美元）",
            "Verizon and Corning have reached a multi-billion dollar supply agreement for "
            "high-density optical fiber to support Gen AI and hyperscaler long-haul backbone.",
            "ir:VZ",
        ),
    ),
]


def test_positive_backtest_not_hard_rejected():
    for ticker, _ret, _hist, item in POSITIVE_SAMPLES:
        reject, reason = hard_reject_deal_item(item)
        assert reject is False, f"{ticker} hard-rejected: {reason}"


def test_positive_backtest_llm_primary_keeps_score():
    for ticker, _ret, hist_llm, item in POSITIVE_SAMPLES:
        text = f"{item.headline}\n{item.summary}"
        primary = finalize_materiality_score(
            text, item.source, ["LLM"], llm_score=hist_llm, llm_primary=True
        )
        legacy = finalize_materiality_score(
            text, item.source, ["LLM"], llm_score=hist_llm, llm_primary=False
        )
        assert primary >= hist_llm, f"{ticker} primary={primary} < hist_llm={hist_llm}"
        # 至少不应比旧模式更差到掉出 70 门槛（对曾被压死的 normal 档尤其关键）
        if legacy < 70 <= hist_llm:
            assert primary >= 70, f"{ticker} should be rescued by llm_primary (legacy={legacy})"
