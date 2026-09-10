"""nvda_signal 专用配置。"""

from pathlib import Path

from app.config import (
    FINNHUB_API_KEY,
    GEMINI_API_KEY,
    NVDA_SIGNAL_A_DEDUP_DAYS,
    NVDA_SIGNAL_A_PLUS_B_DEDUP_DAYS,
    NVDA_SIGNAL_A_PLUS_B_ENABLED,
    NVDA_SIGNAL_A_PLUS_B_INTRADAY_MAX_GAIN,
    NVDA_SIGNAL_A_PLUS_B_STOP_LOSS,
    NVDA_SIGNAL_A_PLUS_B_POSITION_PCT,
    NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A,
    NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A_PLUS_B,
    NVDA_SIGNAL_ENABLED,
    NVDA_SIGNAL_MIN_MATERIALITY_A,
    NVDA_SIGNAL_MIN_MATERIALITY_A_PLUS_B,
    NVDA_SIGNAL_PRIOR_A_LOOKBACK_DAYS,
    NVDA_SIGNAL_PUSH_ENABLED,
    NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A,
    NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A_PLUS_B,
    NVDA_SIGNAL_USE_LLM,
    SEC_USER_AGENT,
)

NVDA_NEWSROOM_RSS = "https://nvidianews.nvidia.com/releases.xml"

GOOGLE_NEWS_NVDA_QUERIES = [
    '(NVIDIA OR "Jensen Huang" OR NVDA) (invest OR investment OR "strategic partnership" OR '
    '"purchase commitment" OR offtake OR "supply agreement" OR acquire OR acquisition OR '
    '"to acquire" OR "definitive agreement") when:3d',
    '(NVIDIA OR "Jensen Huang") ("trillion" OR "buy their stock" OR "buy the stock" OR '
    '"next trillion") when:7d',
]

# NVDA SEC CIK
NVDA_CIK = "0001045810"

ACTION_MIN_SCORE = {
    "NVDA_ACQUIRE_UNLISTED": 70,
    "NVDA_ACQUIRE": 70,
    "NVDA_INVEST": 72,
    "NVDA_PURCHASE_COMMIT": 72,
    "NVDA_CAPACITY_LOCK": 72,
    "NVDA_STRATEGIC_PARTNER": 60,
    "NVDA_SUPPLY_LT": 68,
    "NVDA_VERBAL_BULLISH": 55,
    "NVDA_VERBAL_BUY": 55,
    "NVDA_VERBAL_DEMAND": 55,
}
