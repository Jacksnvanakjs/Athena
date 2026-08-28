"""NVDA 信号材料性与置信度评分。"""

from __future__ import annotations

import re

from app.database import NvdaSignalEvent
from app.nvda_signal.config import ACTION_MIN_SCORE
from app.nvda_signal.keywords import _norm

SOURCE_WEIGHT = {
    "nvidia_newsroom": 1.0,
    "sec_8k": 1.0,
    "pr_newswire": 0.95,
    "business_tech": 0.95,
    "google_news": 0.85,
    "finnhub": 0.80,
}


def _amount_boost(blob: str) -> int:
    score = 0
    if re.search(r"\$\s*\d+(\.\d+)?\s*(billion|b\b)", blob):
        score += 25
    elif re.search(r"\$\s*\d+(\.\d+)?\s*(million|m\b)", blob):
        score += 15
    if "multi-year" in blob or "多年" in blob:
        score += 10
    if "item 1.01" in blob or "definitive agreement" in blob:
        score += 15
    return score


def score_a(blob: str, source: str, action_type: str) -> tuple[int, int]:
    base = 40
    base += int(SOURCE_WEIGHT.get(source, 0.75) * 20)
    base += _amount_boost(blob)
    if "nvidia" in blob and ("partnership" in blob or "invest" in blob):
        base += 10
    materiality = min(100, base)
    confidence = min(100, materiality + 5)
    floor = ACTION_MIN_SCORE.get(action_type, 65)
    return materiality, confidence if materiality >= floor else max(0, confidence - 10)


def score_a_plus_b(
    blob: str,
    source: str,
    prior: NvdaSignalEvent,
    prior_days: int,
) -> tuple[int, int]:
    score = 0
    if prior.materiality_score >= 85:
        score += 30
    elif prior.materiality_score >= 70:
        score += 22
    else:
        score += 15

    if prior_days <= 30:
        score += 20
    elif prior_days <= 60:
        score += 15
    else:
        score += 10

    if re.search(r"\b[A-Z]{2,5}\b", blob) or "marvell" in blob or "lite" in blob:
        score += 25
    else:
        score += 12

    score += int(SOURCE_WEIGHT.get(source, 0.75) * 10)
    materiality = min(100, score)
    confidence = min(100, materiality + 8)
    return materiality, confidence
