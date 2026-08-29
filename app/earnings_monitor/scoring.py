"""财报催化剂评分：硬淘汰 + 六维简化规则分（v1，无 LLM）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.earnings_monitor.config import (
    DEAL_T0_MIN_CAP,
    EARNINGS_PUSH_MIN_SCORE,
    EARNINGS_SCORE_LOOKAHEAD_DAYS,
    SECTOR_LABELS,
)

SECTOR_BASE = {
    "AI_SEC": 18,
    "AI_INFRA": 16,
    "AI_SEMI": 16,
    "AI_SAAS": 14,
    "AI_NET": 12,
}


@dataclass
class ScoreResult:
    score_total: int | None
    score_detail_json: str | None
    eliminate_reason: str | None
    push_eligible: bool
    one_liner: str
    risk_oneliner: str
    pre_30d_gain: float | None = None


async def fetch_pre_30d_gain(ticker: str) -> float | None:
    """相对约 30 日前收盘的涨幅（小数，如 0.12 = +12%）。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "2mo", "interval": "1d"}
    headers = {"User-Agent": "Mozilla/5.0 AthenaEarningsMonitor/1.0"}
    try:
        async with httpx.AsyncClient(headers=headers, timeout=20) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            result = (resp.json().get("chart") or {}).get("result") or []
            if not result:
                return None
            closes = (result[0].get("indicators") or {}).get("quote", [{}])[0].get("close") or []
            closes = [c for c in closes if c is not None]
            if len(closes) < 5:
                return None
            # 约 21 个交易日 ≈ 30 自然日
            lookback = min(21, len(closes) - 1)
            old = closes[-(lookback + 1)]
            latest = closes[-1]
            if not old or old <= 0:
                return None
            return (latest - old) / old
    except Exception as exc:
        logger.debug("30日涨幅 %s 失败: %s", ticker, exc)
        return None


def hard_eliminate(
    *,
    tier: str,
    market_cap_usd: float | None,
    pre_30d_gain: float | None,
) -> str | None:
    if tier == "T0" or (market_cap_usd is not None and market_cap_usd >= DEAL_T0_MIN_CAP):
        return "E1:市值T0巨头排除"
    if tier == "UNKNOWN" and market_cap_usd is None:
        return "E2:无市值无法分档"
    if pre_30d_gain is not None and pre_30d_gain > 0.25:
        return f"E5:财报前30日涨幅{pre_30d_gain * 100:.0f}%>25%"
    return None


def score_candidate(
    *,
    sector: str,
    tier: str,
    session: str,
    confirmed: bool,
    days_to: int,
    pre_30d_gain: float | None,
    eliminate_reason: str | None,
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
        )

    # A 赛道
    a = SECTOR_BASE.get(sector, 10)
    # B 市值档：T1 更优
    b = 18 if tier == "T1" else (12 if tier == "T2" else 8)
    # C 时段明确
    c = 14 if session in ("AMC", "BMO") else 6
    # D 日期确认
    d = 12 if confirmed else 6
    # E 位置：涨幅越低越好
    if pre_30d_gain is None:
        e = 10
    elif pre_30d_gain <= 0.05:
        e = 18
    elif pre_30d_gain <= 0.12:
        e = 14
    elif pre_30d_gain <= 0.20:
        e = 8
    else:
        e = 4
    # F 临近度：越近越高（仍可展示）
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
        "B_tier": b,
        "C_session": c,
        "D_confirmed": d,
        "E_position": e,
        "F_proximity": f,
        "pre_30d_gain": pre_30d_gain,
        "days_to": days_to,
    }

    sess_cn = {"AMC": "盘后", "BMO": "盘前", "TBD": "时段待定"}.get(session, session)
    sector_cn = SECTOR_LABELS.get(sector, sector)
    gain_txt = (
        f"近30日{'+' if (pre_30d_gain or 0) >= 0 else ''}{(pre_30d_gain or 0) * 100:.0f}%"
        if pre_30d_gain is not None
        else "近30日涨幅未知"
    )
    one = f"{sector_cn} {tier} · {sess_cn} · {gain_txt} · 距今{days_to}天"
    risk = "财报前勿埋伏；发布后跳空≥15%不追；beat 不代表必涨"
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
    )
