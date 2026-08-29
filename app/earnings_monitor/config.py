"""earnings_monitor 专用配置（复用 app.config）。"""

from pathlib import Path

from app.config import (
    BASE_DIR,
    DATA_DIR,
    DEAL_T0_MIN_CAP,
    DEAL_T1_MIN_CAP,
    EARNINGS_CALENDAR_REFRESH_HOURS,
    EARNINGS_CALENDAR_SOURCE,
    EARNINGS_CHASE_GAP_PCT_BLOCK,
    EARNINGS_HOLD_TRADING_DAYS_MAX,
    EARNINGS_LOOKAHEAD_DAYS,
    EARNINGS_MONITOR_ENABLED,
    EARNINGS_PUSH_ALLOW_T_DAY,
    EARNINGS_PUSH_ALLOW_T_MINUS_1,
    EARNINGS_PUSH_DAYS_BEFORE,
    EARNINGS_PUSH_ENABLED,
    EARNINGS_PUSH_MIN_SCORE,
    EARNINGS_SCORE_LOOKAHEAD_DAYS,
    EARNINGS_STRATEGY,
    EARNINGS_WEB_MIN_SCORE,
    FINNHUB_API_KEY,
)

UNIVERSE_CANDIDATES = (
    Path(__file__).resolve().parent / "earnings_universe.json",
    DATA_DIR / "earnings_universe.json",
    BASE_DIR / "data" / "earnings_universe.json",
)

SECTOR_LABELS = {
    "AI_SEC": "AI安全",
    "AI_INFRA": "AI基建",
    "AI_SEMI": "AI半导体",
    "AI_SAAS": "AI应用",
    "AI_NET": "AI网络",
}

STRATEGY_LABEL = "财报后买 / 2日内卖"
