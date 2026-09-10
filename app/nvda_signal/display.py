"""黄仁勋质量标签：与 AI 合作共用 QUALITY_* / 中文名。"""

from __future__ import annotations

from app.deal_monitor.materiality import (
    QUALITY_HARD,
    QUALITY_MA,
    QUALITY_NORMAL,
    QUALITY_VERBAL,
    deal_quality_label,
)

# 内部 action → 与 AI 合作同一套 quality key
_ACTION_QUALITY: dict[str, str] = {
    "NVDA_ACQUIRE_UNLISTED": QUALITY_MA,
    "NVDA_ACQUIRE": QUALITY_MA,
    "NVDA_INVEST": QUALITY_HARD,
    "NVDA_PURCHASE_COMMIT": QUALITY_HARD,
    "NVDA_CAPACITY_LOCK": QUALITY_HARD,
    "NVDA_SUPPLY_LT": QUALITY_HARD,
    "NVDA_STRATEGIC_PARTNER": QUALITY_HARD,
    "NVDA_VERBAL_BULLISH": QUALITY_VERBAL,
    "NVDA_VERBAL_BUY": QUALITY_VERBAL,
    "NVDA_VERBAL_DEMAND": QUALITY_VERBAL,
}


def nvda_quality_key(action_type: str | None, signal_tier: str | None) -> str:
    if (signal_tier or "").upper() == "A_PLUS_B":
        return QUALITY_VERBAL
    key = (action_type or "").upper()
    if key in _ACTION_QUALITY:
        return _ACTION_QUALITY[key]
    if (signal_tier or "").upper() == "A":
        return QUALITY_HARD
    return QUALITY_NORMAL


def nvda_display_quality(action_type: str | None, signal_tier: str | None) -> tuple[str, str]:
    """返回 (deal_quality, deal_quality_label)，文案与 AI 合作一致。"""
    q = nvda_quality_key(action_type, signal_tier)
    return q, deal_quality_label(q)
