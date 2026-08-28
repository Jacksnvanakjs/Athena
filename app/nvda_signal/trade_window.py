"""买卖窗口策略：EARLIEST_BUY_T1_CLOSE / INTRADAY_FAST_EXIT。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from app.nvda_signal.config import (
    NVDA_SIGNAL_A_PLUS_B_INTRADAY_MAX_GAIN,
    NVDA_SIGNAL_A_PLUS_B_POSITION_PCT,
    NVDA_SIGNAL_A_PLUS_B_STOP_LOSS,
    NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A,
    NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A_PLUS_B,
)


@dataclass
class TradePlan:
    strategy: str
    buy_window: str
    sell_window: str
    sell_plan_json: str
    position_pct: float
    buy_ok: bool
    chase_risk: str  # low | caution | high


STRATEGY_LABELS = {
    "EARLIEST_BUY_T1_CLOSE": "尽早买 · 次日收盘卖",
    "INTRADAY_FAST_EXIT": "盘中买 · 快进快出",
    "LEGACY_BATCH": "分批止盈（进阶）",
}


def strategy_label(code: str) -> str:
    return STRATEGY_LABELS.get(code, code)


def build_trade_plan(
    signal_tier: str,
    published_at: datetime,
    gap_pct: float | None = None,
    pre_30d_gain: float | None = None,
    pre_10d_gain: float | None = None,
    intraday_gain: float | None = None,
) -> TradePlan:
    gap = gap_pct or 0.0
    g30 = pre_30d_gain or 0.0
    g10 = pre_10d_gain or 0.0
    intra = intraday_gain or 0.0

    if signal_tier == "A_PLUS_B":
        plan_json = {
            "strategy": "INTRADAY_FAST_EXIT",
            "buy_windows": ["intraday_within_30min"],
            "buy_conditions": {"max_intraday_gain_pct": NVDA_SIGNAL_A_PLUS_B_INTRADAY_MAX_GAIN * 100},
            "sell_at": "same_day_close_preferred",
            "sell_fallback": "next_open_first_30min",
            "sell_label": "当日收盘优先，否则次日早盘清仓",
            "no_chase_if_gap_pct": NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A_PLUS_B * 100,
            "stop_loss_pct": NVDA_SIGNAL_A_PLUS_B_STOP_LOSS * 100,
            "position_pct": NVDA_SIGNAL_A_PLUS_B_POSITION_PCT,
        }
        buy_window = "盘中 30 分钟内；当日涨幅<10%"
        sell_window = "当日收盘优先，否则次日早盘 30 分钟内清仓"
        buy_ok = True
        chase_risk = "low"
        if gap >= NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A_PLUS_B:
            buy_ok = False
            chase_risk = "high"
        elif intra >= NVDA_SIGNAL_A_PLUS_B_INTRADAY_MAX_GAIN:
            buy_ok = False
            chase_risk = "high"
        elif g30 > 0.25:
            buy_ok = False
            chase_risk = "high"
        return TradePlan(
            strategy="INTRADAY_FAST_EXIT",
            buy_window=buy_window,
            sell_window=sell_window,
            sell_plan_json=json.dumps(plan_json, ensure_ascii=False),
            position_pct=NVDA_SIGNAL_A_PLUS_B_POSITION_PCT,
            buy_ok=buy_ok,
            chase_risk=chase_risk,
        )

    plan_json = {
        "strategy": "EARLIEST_BUY_T1_CLOSE",
        "buy_windows": ["after_hours", "pre_market", "next_open_0935_1000"],
        "sell_at": "next_trading_day_close",
        "sell_label": "次日收盘全部卖出",
        "no_chase_if_gap_pct": NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A * 100,
        "optional_stop_loss_pct": -8,
        "position_pct": 1.0,
    }
    buy_window = "盘后/盘前/次日 09:35-10:00 ET（跳空<15%）"
    sell_window = "买入后第 1 个交易日收盘清仓"
    buy_ok = True
    chase_risk = "low"
    if gap >= NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A:
        buy_ok = False
        chase_risk = "high"
    elif g30 > 0.25:
        buy_ok = False
        chase_risk = "high"
    elif g10 > 0.15:
        buy_ok = True
        chase_risk = "caution"
        plan_json["position_pct"] = 0.5
    elif intra >= 0.08:
        buy_ok = False
        chase_risk = "high"

    return TradePlan(
        strategy="EARLIEST_BUY_T1_CLOSE",
        buy_window=buy_window,
        sell_window=sell_window,
        sell_plan_json=json.dumps(plan_json, ensure_ascii=False),
        position_pct=float(plan_json.get("position_pct", 1.0)),
        buy_ok=buy_ok,
        chase_risk=chase_risk,
    )
