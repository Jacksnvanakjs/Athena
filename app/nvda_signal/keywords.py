"""A / A_PLUS_B / B / C 档关键词。"""

from __future__ import annotations

import re

NVDA_MARKERS = (
    "nvidia", "nvda", "jensen huang", "黄仁勋", "英伟达",
)

A_HARD_TOKENS = (
    "invest", "investment", "equity stake", "warrants",
    "strategic partnership", "multi-year", "purchase commitment",
    "offtake", "capacity", "pre-pay", "prepayment", "supply agreement",
    "long-term agreement", "collaboration agreement", "co-develop",
    "joint development", "fabrication facility", "manufacturing",
    "allocation", "reserved capacity", "slot reservation",
    "$ billion", "billion-dollar", "亿美元", "战略合作", "长期协议", "产能", "投资",
    "item 1.01", "material definitive agreement", "definitive agreement",
)

VERBAL_B_TOKENS = (
    "trillion-dollar", "trillion dollar", "next trillion", "万亿",
    "buy the stock", "buy their stock", "打折买入",
    "essential", "doing so well", "please make more", "多生产",
    "buy their shares", "strong demand",
)

C_TIER_TOKENS = (
    "dinner", "lunch", "meal", "restaurant", "饭局", "炸鸡", "烤五花肉",
    "korean bbq", "seoul dinner",
)

RUMOR_TOKENS = (
    "according to people familiar", "reportedly in talks", "reportedly", "reports said",
    "the information reported", "the information's report", "sources say",
    "in talks to", "in talks with", "in talks",
    "据悉", "传闻", "或将", "拟",
    "people familiar with",
)

INVEST_TOKENS = ("invest", "investment", "equity stake", "warrants", "投资")
PURCHASE_TOKENS = ("purchase commitment", "offtake", "multi-year purchase", "采购承诺")
CAPACITY_TOKENS = (
    "capacity", "reserved capacity", "slot reservation", "fabrication facility",
    "产能", "建厂",
)
SUPPLY_TOKENS = ("supply agreement", "long-term agreement", "long-term supply", "长期供应")
PARTNER_TOKENS = ("strategic partnership", "collaboration agreement", "战略合作", "合作协议")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def has_nvda(text: str) -> bool:
    blob = _norm(text)
    return any(m in blob for m in NVDA_MARKERS)


def has_any(blob: str, tokens: tuple[str, ...]) -> bool:
    return any(tok in blob for tok in tokens)


def is_c_tier(blob: str) -> bool:
    return has_any(blob, C_TIER_TOKENS)


def is_rumor(blob: str) -> bool:
    return has_any(blob, RUMOR_TOKENS)


def has_a_hard_terms(blob: str) -> bool:
    return has_any(blob, A_HARD_TOKENS)


def has_verbal_terms(blob: str) -> bool:
    return has_any(blob, VERBAL_B_TOKENS)


def detect_action_type(blob: str, signal_tier: str) -> str:
    if signal_tier == "A_PLUS_B":
        if has_any(blob, ("buy the stock", "buy their stock", "buy their shares", "打折买入")):
            return "NVDA_VERBAL_BUY"
        if has_any(blob, ("please make more", "多生产", "strong demand")):
            return "NVDA_VERBAL_DEMAND"
        return "NVDA_VERBAL_BULLISH"
    if has_any(blob, INVEST_TOKENS):
        return "NVDA_INVEST"
    if has_any(blob, PURCHASE_TOKENS):
        return "NVDA_PURCHASE_COMMIT"
    if has_any(blob, CAPACITY_TOKENS):
        return "NVDA_CAPACITY_LOCK"
    if has_any(blob, SUPPLY_TOKENS):
        return "NVDA_SUPPLY_LT"
    if has_any(blob, PARTNER_TOKENS):
        return "NVDA_STRATEGIC_PARTNER"
    return "NVDA_STRATEGIC_PARTNER"
