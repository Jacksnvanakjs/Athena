"""市值分档与推送角色判定。"""

from __future__ import annotations

from dataclasses import dataclass

from app.deal_monitor.config import (
    DEAL_T0_MIN_CAP,
    DEAL_T0_T0_PUSH_ENABLED,
    DEAL_T1_MIN_CAP,
    DEAL_T2_T2_PUSH_BOTH,
    SCORE_THRESHOLDS,
    UNLISTED_T0_MARKET_CAP,
)
from app.deal_monitor.entities import Entity, registry


def classify_tier(
    market_cap_usd: float | None,
    ticker: str | None = None,
    unlisted_id: str | None = None,
) -> str:
    if unlisted_id:
        return "T0"
    if ticker and registry.is_t0_listed_seed(ticker):
        return "T0"
    if market_cap_usd is None:
        return "UNKNOWN"
    if market_cap_usd >= DEAL_T0_MIN_CAP:
        return "T0"
    if market_cap_usd >= DEAL_T1_MIN_CAP:
        return "T1"
    return "T2"


def effective_market_cap(entity: Entity) -> float:
    if entity.unlisted_id:
        return UNLISTED_T0_MARKET_CAP
    if entity.market_cap_usd is not None:
        return entity.market_cap_usd
    if entity.ticker and registry.is_t0_listed_seed(entity.ticker):
        return UNLISTED_T0_MARKET_CAP
    return 0.0


def format_tier_pair(tier_a: str, tier_b: str) -> str:
    order = {"T0": 0, "T1": 1, "T2": 2, "UNKNOWN": 3}
    ta, tb = sorted([tier_a, tier_b], key=lambda t: order.get(t, 9))
    return f"{ta}_{tb}"


def score_threshold(tier_pair: str) -> int:
    return SCORE_THRESHOLDS.get(tier_pair, SCORE_THRESHOLDS["T0_T2"])


@dataclass
class RoleAssignment:
    anchor: Entity
    beneficiary: Entity
    tier_pair: str
    should_push: bool
    push_both: bool = False
    skip_reason: str | None = None


def _by_smaller_cap(a: Entity, b: Entity) -> tuple[Entity, Entity]:
    cap_a = effective_market_cap(a)
    cap_b = effective_market_cap(b)
    if cap_a <= cap_b:
        return a, b
    return b, a


def _pick_by_tier(a: Entity, b: Entity, beneficiary_tier: str) -> tuple[Entity, Entity]:
    if a.tier == beneficiary_tier:
        return b, a
    return a, b


def assign_roles(entity_a: Entity, entity_b: Entity) -> RoleAssignment | None:
    """按 §2.2 判定锚点、受益方与是否推送。"""
    tier_a = entity_a.tier
    tier_b = entity_b.tier
    tier_pair = format_tier_pair(tier_a, tier_b)

    if "UNKNOWN" in tier_pair:
        if tier_a == "UNKNOWN" and tier_b == "UNKNOWN":
            return None
        return RoleAssignment(
            anchor=entity_a if tier_a != "UNKNOWN" else entity_b,
            beneficiary=entity_b if tier_a != "UNKNOWN" else entity_a,
            tier_pair=tier_pair,
            should_push=False,
            skip_reason="含 UNKNOWN 不推",
        )

    beneficiary: Entity
    anchor: Entity

    if tier_pair == "T0_T2":
        anchor, beneficiary = _pick_by_tier(entity_a, entity_b, "T2")
    elif tier_pair == "T0_T1":
        anchor, beneficiary = _pick_by_tier(entity_a, entity_b, "T1")
    elif tier_pair == "T1_T2":
        anchor, beneficiary = _pick_by_tier(entity_a, entity_b, "T2")
    elif tier_pair in ("T0_T0", "T1_T1", "T2_T2"):
        beneficiary, anchor = _by_smaller_cap(entity_a, entity_b)
    else:
        beneficiary, anchor = _by_smaller_cap(entity_a, entity_b)

    should_push = True
    push_both = False
    skip_reason = None

    if tier_pair == "T0_T0" and not DEAL_T0_T0_PUSH_ENABLED:
        should_push = False
        skip_reason = "T0↔T0 推送已关闭"
    elif tier_pair == "T2_T2":
        push_both = DEAL_T2_T2_PUSH_BOTH

    if not beneficiary.ticker:
        should_push = False
        skip_reason = skip_reason or "受益方无 ticker"

    return RoleAssignment(
        anchor=anchor,
        beneficiary=beneficiary,
        tier_pair=tier_pair,
        should_push=should_push,
        push_both=push_both,
        skip_reason=skip_reason,
    )
