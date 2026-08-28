"""A / A_PLUS_B / B / C 四档判定。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.nvda_signal.keywords import (
    detect_action_type,
    has_a_hard_terms,
    has_nvda,
    has_verbal_terms,
    is_c_tier,
    is_rumor,
    _norm,
)

logger = logging.getLogger(__name__)


@dataclass
class SignalClassification:
    signal_tier: str  # A, A_PLUS_B, B, C
    action_type: str
    status: str  # confirmed, rumor
    beneficiary_role: str  # direct | indirect
    reason: str = ""


def classify_signal(text: str, has_prior_a: bool) -> SignalClassification | None:
    blob = _norm(text)
    if not has_nvda(blob):
        return None

    if is_c_tier(blob):
        logger.debug("NVDA signal C tier: %s", text[:80])
        return SignalClassification("C", "NVDA_VERBAL_BULLISH", "confirmed", "direct", "C档饭局/行程")

    rumor = is_rumor(blob)

    if has_a_hard_terms(blob):
        action = detect_action_type(blob, "A")
        return SignalClassification(
            "A",
            action,
            "rumor" if rumor else "confirmed",
            "direct",
            "A档硬条款",
        )

    if has_verbal_terms(blob):
        if has_prior_a:
            action = detect_action_type(blob, "A_PLUS_B")
            return SignalClassification(
                "A_PLUS_B",
                action,
                "rumor" if rumor else "confirmed",
                "direct",
                "90天内有A档，口头催化升格",
            )
        return SignalClassification("B", "NVDA_VERBAL_BULLISH", "confirmed", "direct", "纯B档口头，无前期A")

    return None
