"""NVDA 信号材料性与置信度评分。

黄仁勋独立刻度（不与 AI 合作并购封顶混用）：
- 并购：有金额+正式协议 → 约 78–82；无商业条款 → ≤58
- 硬催化（投资/产能等）：≤85，有条款地板 ≥72
- 口头催化：≤62
"""

from __future__ import annotations

import re

from app.database import NvdaSignalEvent
from app.deal_monitor.materiality import QUALITY_HARD, QUALITY_MA, QUALITY_VERBAL
from app.nvda_signal.config import ACTION_MIN_SCORE
from app.nvda_signal.display import nvda_quality_key

SOURCE_WEIGHT = {
    "nvidia_newsroom": 1.0,
    "sec_8k": 1.0,
    "pr_newswire": 0.95,
    "business_tech": 0.95,
    "google_news": 0.85,
    "finnhub": 0.80,
}

_QUALITY_CAP = {
    QUALITY_MA: 82,
    QUALITY_HARD: 85,
    QUALITY_VERBAL: 62,
}

_HARD_FLOOR_ACTIONS = frozenset({
    "NVDA_INVEST",
    "NVDA_PURCHASE_COMMIT",
    "NVDA_CAPACITY_LOCK",
    "NVDA_SUPPLY_LT",
})


def _amount_boost(blob: str) -> int:
    score = 0
    if re.search(r"\$\s*\d+(\.\d+)?\s*(billion|b\b)", blob):
        score += 14
    elif re.search(r"\$\s*\d+(\.\d+)?\s*(million|m\b)", blob):
        score += 9
    if "multi-year" in blob or "多年" in blob:
        score += 6
    if "item 1.01" in blob or "definitive agreement" in blob:
        score += 10
    return score


def _has_commercial(blob: str) -> bool:
    return bool(
        re.search(r"\$\s*\d+", blob)
        or "multi-year" in blob
        or "多年" in blob
        or "item 1.01" in blob
        or "definitive agreement" in blob
    )


def _strong_ma(blob: str) -> bool:
    """巨额或正式协议的收购：抬到 78–82。"""
    has_b = bool(re.search(r"\$\s*\d+(\.\d+)?\s*(billion|b\b)", blob))
    has_def = "item 1.01" in blob or "definitive agreement" in blob
    return has_b or (has_def and bool(re.search(r"\$\s*\d+", blob)))


def _confidence_a(blob: str, source: str, materiality: int, floor: int) -> int:
    """推送门槛用；列表不展示。"""
    conf = 55
    conf += int(SOURCE_WEIGHT.get(source, 0.75) * 30)
    if "item 1.01" in blob or "definitive agreement" in blob:
        conf += 8
    if re.search(r"\$\s*\d+(\.\d+)?\s*(billion|million|b\b|m\b)", blob):
        conf += 4
    if source == "sec_8k":
        conf = min(98, conf)
    else:
        conf = min(92, conf)
    if materiality < floor:
        conf = max(0, conf - 10)
    return conf


def score_a(blob: str, source: str, action_type: str) -> tuple[int, int]:
    """黄仁勋材料性：强并购 78–82；硬催化可到 85。"""
    base = 48
    base += int(SOURCE_WEIGHT.get(source, 0.75) * 14)
    base += _amount_boost(blob)
    if "nvidia" in blob and (
        "partnership" in blob
        or "invest" in blob
        or "acquir" in blob
        or "并购" in blob
        or "收购" in blob
    ):
        base += 8

    quality = nvda_quality_key(action_type, "A")
    cap = _QUALITY_CAP.get(quality, 70)

    if quality == QUALITY_MA:
        if _strong_ma(blob):
            materiality = min(cap, max(base, 78))
        elif _has_commercial(blob):
            materiality = min(72, base)
        else:
            materiality = min(58, base)
    elif quality == QUALITY_HARD:
        materiality = min(cap, base)
        if action_type in _HARD_FLOOR_ACTIONS and _has_commercial(blob):
            materiality = max(materiality, 72)
        if action_type == "NVDA_STRATEGIC_PARTNER" and not _has_commercial(blob):
            materiality = min(materiality, 68)
    else:
        materiality = min(cap, base)

    floor = ACTION_MIN_SCORE.get(action_type, 65)
    confidence = _confidence_a(blob, source, materiality, floor)
    return materiality, confidence


def score_a_plus_b(
    blob: str,
    source: str,
    prior: NvdaSignalEvent,
    prior_days: int,
) -> tuple[int, int]:
    score = 0
    if prior.materiality_score >= 85:
        score += 28
    elif prior.materiality_score >= 70:
        score += 22
    else:
        score += 15

    if prior_days <= 30:
        score += 18
    elif prior_days <= 60:
        score += 14
    else:
        score += 10

    if re.search(r"\b[A-Z]{2,5}\b", blob) or "marvell" in blob or "lite" in blob:
        score += 16
    else:
        score += 10

    score += int(SOURCE_WEIGHT.get(source, 0.75) * 8)
    materiality = min(_QUALITY_CAP[QUALITY_VERBAL], score)
    confidence = min(90, materiality + 8)
    return materiality, confidence
