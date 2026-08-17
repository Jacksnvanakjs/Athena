"""deal_monitor 专用配置（复用 app.config 中的 DEAL_* 环境变量）。"""

from pathlib import Path

from app.config import (
    DEAL_DEDUP_DAYS,
    DEAL_MAX_PUSH_PER_BENEFICIARY_24H,
    DEAL_MAX_PUSH_PER_HOUR,
    DEAL_POLL_INTERVAL_MIN,
    DEAL_PUSH_ENABLED,
    DEAL_SCORE_MIN_DEFAULT,
    DEAL_SCORE_MIN_T0_T0,
    DEAL_SCORE_MIN_T0_T1,
    DEAL_SCORE_MIN_T1_T1,
    DEAL_T0_MIN_CAP,
    DEAL_T0_T0_PUSH_ENABLED,
    DEAL_T1_MIN_CAP,
    DEAL_T2_MAX_CAP,
    DEAL_T2_T2_PUSH_BOTH,
    FINNHUB_API_KEY,
    SEC_USER_AGENT,
)

ENTITIES_SEED_FILE = Path(__file__).resolve().parent / "entities_seed.json"

# Phase 1 RSS 源
PR_WIRE_FEEDS = [
    {
        "name": "pr_newswire",
        "url": "https://www.prnewswire.com/rss/technology-latest-news/technology-latest-news-list.rss",
    },
    {
        "name": "globe",
        "url": "https://www.globenewswire.com/RssFeed/subjectcode/13-Artificial%20Intelligence/feedTitle/GlobeNewswire%20-%20Artificial%20Intelligence",
    },
]

# 未上市 T0 锚点视为极大市值
UNLISTED_T0_MARKET_CAP = 1e12

SCORE_THRESHOLDS = {
    "T0_T2": DEAL_SCORE_MIN_DEFAULT,
    "T0_T1": DEAL_SCORE_MIN_T0_T1,
    "T1_T2": DEAL_SCORE_MIN_DEFAULT,
    "T0_T0": DEAL_SCORE_MIN_T0_T0,
    "T1_T1": DEAL_SCORE_MIN_T1_T1,
    "T2_T2": DEAL_SCORE_MIN_DEFAULT,
}
