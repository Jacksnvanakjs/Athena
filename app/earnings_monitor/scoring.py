"""财报催化剂评分：以「财报后更可能上涨」为导向（财报前可得信息）。

设计取舍（可解释、可回测）：
- 不再因「市值偏大」硬淘汰 DELL 这类 AI 硬件受益票
- 不再因「30 日已涨 >25%」硬淘汰（GTLB 类动量票可高分）
- 奖励「健康回调」与「建设性动量进财报」；惩罚「滞涨延伸 / 尾盘反抽 / 连续阴跌 / 暴跌进场」
- T0 巨头仍不推送（波动弹性与策略不同）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date

from app.earnings_monitor.config import (
    DEAL_T0_MIN_CAP,
    EARNINGS_PUSH_MIN_SCORE,
    EARNINGS_SCORE_LOOKAHEAD_DAYS,
    SECTOR_LABELS,
)

logger = logging.getLogger(__name__)

# 硬件/基建类财报后弹性通常更好（相对纯软件大票）
SECTOR_BASE = {
    "AI_INFRA": 16,
    "AI_SEMI": 15,
    "AI_NET": 14,
    "AI_SEC": 13,
    "AI_SAAS": 13,
}


@dataclass
class PriceSetupFeatures:
    pre_5d_gain: float | None = None
    pre_10d_gain: float | None = None
    pre_30d_gain: float | None = None
    down_streak: int | None = None
    from_21d_high: float | None = None


@dataclass
class ScoreResult:
    score_total: int | None
    score_detail_json: str | None
    eliminate_reason: str | None
    push_eligible: bool
    one_liner: str
    risk_oneliner: str
    pre_30d_gain: float | None = None
    pre_10d_gain: float | None = None


async def fetch_pre_nd_gain(
    ticker: str,
    *,
    sessions: int = 10,
    as_of: date | None = None,
) -> float | None:
    """相对 as_of 之前约 N 个交易日收盘的涨幅。"""
    from app.market_data import fetch_daily_closes

    closes = await fetch_daily_closes(ticker, lookback_days=max(60, sessions + 40))
    if as_of is not None:
        closes = [(d, c) for d, c in closes if d <= as_of]
    if len(closes) < sessions + 1:
        return None
    old = closes[-(sessions + 1)][1]
    latest = closes[-1][1]
    if not old or old <= 0:
        return None
    return (latest - old) / old


async def fetch_pre_30d_gain(
    ticker: str,
    as_of: date | None = None,
) -> float | None:
    """相对约 21 个交易日（≈30 自然日）前收盘的涨幅。"""
    return await fetch_pre_nd_gain(ticker, sessions=21, as_of=as_of)


async def fetch_price_setup_features(
    ticker: str,
    as_of: date | None = None,
) -> PriceSetupFeatures:
    """一次拉日线，计算进财报前的多维价格形态。"""
    from app.market_data import fetch_daily_closes

    closes = await fetch_daily_closes(ticker, lookback_days=80)
    if as_of is not None:
        closes = [(d, c) for d, c in closes if d <= as_of]
    if len(closes) < 6:
        return PriceSetupFeatures()

    def _gain(sessions: int) -> float | None:
        if len(closes) < sessions + 1:
            return None
        old = closes[-(sessions + 1)][1]
        latest = closes[-1][1]
        if not old or old <= 0:
            return None
        return (latest - old) / old

    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i][1] < closes[i - 1][1]:
            streak += 1
        else:
            break

    window = closes[-21:] if len(closes) >= 21 else closes
    hi = max(c for _, c in window)
    last = closes[-1][1]
    from_hi = (last / hi - 1.0) if hi and hi > 0 else None

    return PriceSetupFeatures(
        pre_5d_gain=_gain(5),
        pre_10d_gain=_gain(10),
        pre_30d_gain=_gain(21),
        down_streak=streak,
        from_21d_high=from_hi,
    )


def hard_eliminate(
    *,
    tier: str,
    market_cap_usd: float | None,
    pre_30d_gain: float | None,
    pre_10d_gain: float | None = None,
) -> str | None:
    if tier == "T0" or (market_cap_usd is not None and market_cap_usd >= DEAL_T0_MIN_CAP):
        return "E1:市值T0巨头排除"
    if tier == "UNKNOWN" and market_cap_usd is None:
        return "E2:无市值无法分档"
    # 注意：不再用「30日涨幅>25%」硬淘汰（E5）。GTLB 类建设性动量可高分，
    # 真正的追高/滞涨由 setup 负分处理（见 stale_extension / extended_chase）。
    _ = pre_30d_gain
    # 财报前暴跌进场：常继续杀（如 CRDO），与「健康回调」相反
    if pre_10d_gain is not None and pre_10d_gain <= -0.15:
        return f"E7:财报前10日暴跌{pre_10d_gain * 100:.0f}%≤-15%，追跌风险高"
    return None


def _liquidity_score(market_cap_usd: float | None, tier: str) -> int:
    """流动性/可交易性：中大盘 AI 硬件亦可高分；过小票略降。"""
    if market_cap_usd is None:
        return 10 if tier != "T2" else 8
    bil = market_cap_usd / 1e9
    if 20 <= bil < 400:
        return 16  # DELL 等 AI 服务器受益区间
    if 5 <= bil < 20:
        return 15
    if 2 <= bil < 5:
        return 12
    if bil < 2:
        return 8
    return 12  # 400–500B 未到 T0：可交易但弹性一般


def _setup_into_er_score(
    pre_10d_gain: float | None,
    pre_30d_gain: float | None,
    *,
    pre_5d_gain: float | None = None,
    down_streak: int | None = None,
    from_21d_high: float | None = None,
) -> tuple[int, str]:
    """进财报前的价格形态：可为负分，拉开赢家/输家。

    经验对照（2026-09 样本，财报前特征 → 揭晓后）：
    - GTLB 建设性动量 → 涨；DELL/SNOW 健康回调 → 涨
    - MDB 滞涨延伸、PANW 尾盘反抽、HPE 高位磨弱、NTAP 连续阴跌 → 跌
    - CRDO 暴跌进场 → 继续杀（另有 E7）
    """
    g10 = pre_10d_gain if pre_10d_gain is not None else pre_30d_gain
    if g10 is None:
        return 8, "unknown"

    # 1) 暴跌进场
    if g10 <= -0.15:
        return -15, "crash"

    # 2) 连续阴跌进财报（≠健康回调）：如 NTAP
    if down_streak is not None and down_streak >= 4 and g10 < -0.04:
        return -12, "bleeding"

    # 3) 健康回调吸筹：如 DELL / SNOW
    if -0.15 < g10 <= -0.04 and (down_streak is None or down_streak <= 3):
        return 22, "healthy_pullback"

    # 4) 建设性动量进财报（近 10 日仍上行，非硬杀）：如 GTLB
    if 0.05 < g10 <= 0.18 and (pre_30d_gain is None or pre_30d_gain < 0.45):
        return 20, "constructive_momentum"

    # 5) 滞涨延伸：月线已大涨，近 10 日走平 → 如 MDB
    if (
        pre_30d_gain is not None
        and pre_30d_gain > 0.15
        and -0.03 <= g10 <= 0.05
    ):
        return -10, "stale_extension"

    # 6) 尾盘反抽进财报：近 5 日急拉、10 日仍弱 → 如 PANW
    if pre_5d_gain is not None and pre_5d_gain > 0.05 and g10 < 0:
        return -8, "late_bounce"

    # 7) 高位下方磨弱：离 21 日高点深、近 10 日近乎走平 → 如 HPE
    if (
        from_21d_high is not None
        and from_21d_high <= -0.12
        and -0.05 < g10 < 0.05
    ):
        return -8, "weak_off_highs"

    # 8) 极端追高（软惩罚，不硬淘汰）
    if g10 > 0.18 or (
        pre_30d_gain is not None and pre_30d_gain > 0.35 and g10 > 0.10
    ):
        return 4, "extended_chase"

    # 9) 其余分段
    if -0.04 < g10 <= 0.05:
        return 10, "neutral"
    if 0.05 < g10 <= 0.15:
        return 10, "mild_up"
    return 8, "other"


def _fmt_cap(market_cap_usd: float | None) -> str:
    if market_cap_usd is None:
        return ""
    return f"(${market_cap_usd / 1e9:.0f}B)"


def score_candidate(
    *,
    sector: str,
    tier: str,
    session: str,
    confirmed: bool,
    days_to: int,
    pre_30d_gain: float | None,
    eliminate_reason: str | None,
    market_cap_usd: float | None = None,
    pre_10d_gain: float | None = None,
    pre_5d_gain: float | None = None,
    down_streak: int | None = None,
    from_21d_high: float | None = None,
) -> ScoreResult:
    if eliminate_reason:
        return ScoreResult(
            score_total=None,
            score_detail_json=None,
            eliminate_reason=eliminate_reason,
            push_eligible=False,
            one_liner="已硬淘汰，仅作日历参考",
            risk_oneliner=eliminate_reason,
            pre_30d_gain=pre_30d_gain,
            pre_10d_gain=pre_10d_gain,
        )

    if days_to > EARNINGS_SCORE_LOOKAHEAD_DAYS:
        return ScoreResult(
            score_total=None,
            score_detail_json=None,
            eliminate_reason=None,
            push_eligible=False,
            one_liner="距财报较远，待近14日深度评分",
            risk_oneliner="分数尚未计算，勿提前埋伏",
            pre_30d_gain=pre_30d_gain,
            pre_10d_gain=pre_10d_gain,
        )

    a = SECTOR_BASE.get(sector, 12)
    b = _liquidity_score(market_cap_usd, tier)
    c = 14 if session in ("AMC", "BMO") else 6
    d = 12 if confirmed else 6
    e, setup_label = _setup_into_er_score(
        pre_10d_gain,
        pre_30d_gain,
        pre_5d_gain=pre_5d_gain,
        down_streak=down_streak,
        from_21d_high=from_21d_high,
    )
    if days_to <= 2:
        f = 16
    elif days_to <= 5:
        f = 12
    elif days_to <= 10:
        f = 8
    else:
        f = 5

    total = max(0, min(100, a + b + c + d + e + f))
    detail = {
        "A_sector": a,
        "B_liquidity": b,
        "C_session": c,
        "D_confirmed": d,
        "E_setup": e,
        "E_setup_label": setup_label,
        "F_proximity": f,
        "pre_5d_gain": pre_5d_gain,
        "pre_10d_gain": pre_10d_gain,
        "pre_30d_gain": pre_30d_gain,
        "down_streak": down_streak,
        "from_21d_high": from_21d_high,
        "days_to": days_to,
        "market_cap_usd": market_cap_usd,
    }

    sess_cn = {"AMC": "盘后", "BMO": "盘前", "TBD": "时段待定"}.get(session, session)
    sector_cn = SECTOR_LABELS.get(sector, sector)
    setup_cn = {
        "healthy_pullback": "健康回调",
        "constructive_momentum": "建设性动量",
        "stale_extension": "滞涨延伸",
        "late_bounce": "尾盘反抽",
        "weak_off_highs": "高位磨弱",
        "bleeding": "连续阴跌",
        "crash": "暴跌进场",
        "extended_chase": "追高延伸",
        "neutral": "近乎走平",
        "mild_up": "温和上行",
        "unknown": "形态未知",
        "other": "其他形态",
    }.get(setup_label, setup_label)
    g10 = (
        f"10日{'+' if (pre_10d_gain or 0) >= 0 else ''}{(pre_10d_gain or 0) * 100:.0f}%"
        if pre_10d_gain is not None
        else "10日涨幅未知"
    )
    cap_txt = _fmt_cap(market_cap_usd)
    tier_txt = f"{tier}{cap_txt}" if cap_txt else tier
    one = f"{sector_cn} {tier_txt} · {sess_cn} · {setup_cn} · {g10} · 距今{days_to}天"
    risk = "导向：财报后上涨弹性；非保证涨跌；跳空≥15%不追；财报前勿埋伏"
    if not confirmed:
        risk = "日期待确认，可能改期；" + risk

    return ScoreResult(
        score_total=total,
        score_detail_json=json.dumps(detail, ensure_ascii=False),
        eliminate_reason=None,
        push_eligible=total >= EARNINGS_PUSH_MIN_SCORE,
        one_liner=one,
        risk_oneliner=risk,
        pre_30d_gain=pre_30d_gain,
        pre_10d_gain=pre_10d_gain,
    )
