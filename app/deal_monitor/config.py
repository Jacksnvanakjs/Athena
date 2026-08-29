"""deal_monitor 专用配置（复用 app.config 中的 DEAL_* 环境变量）。"""

from pathlib import Path

from app.config import (
    DEAL_DEDUP_DAYS,
    DEAL_MAX_PUSH_PER_BENEFICIARY_24H,
    DEAL_MAX_PUSH_PER_HOUR,
    DEAL_LLM_MODEL,
    DEAL_POLL_INTERVAL_MIN,
    DEAL_PUSH_ENABLED,
    DEAL_PUSH_MAX_AGE_DAYS,
    DEAL_SCORE_MIN_DEFAULT,
    DEAL_SCORE_MIN_T0_T0,
    DEAL_SCORE_MIN_T0_T1,
    DEAL_SCORE_MIN_T1_T1,
    DEAL_T0_MIN_CAP,
    DEAL_T0_T0_PUSH_ENABLED,
    DEAL_T1_MIN_CAP,
    DEAL_T2_MAX_CAP,
    DEAL_T2_T2_PUSH_BOTH,
    DEAL_USE_LLM,
    FINNHUB_API_KEY,
    GEMINI_API_KEY,
    SEC_USER_AGENT,
)

ENTITIES_SEED_FILE = Path(__file__).resolve().parent / "entities_seed.json"
COMPANY_IR_FEEDS_FILE = Path(__file__).resolve().parent / "company_ir_feeds.json"


def _load_company_ir_feeds() -> list[dict[str, str]]:
    import json

    if not COMPANY_IR_FEEDS_FILE.exists():
        return []
    data = json.loads(COMPANY_IR_FEEDS_FILE.read_text(encoding="utf-8"))
    feeds = data.get("feeds") or []
    return [
        {
            "ticker": str(item["ticker"]).upper(),
            "name": item.get("name") or f"ir_{item['ticker'].lower()}",
            "url": str(item["url"]).strip(),
            "category": item.get("category") or "",
        }
        for item in feeds
        if item.get("ticker") and item.get("url")
    ]

# Phase 1 RSS 源
PR_WIRE_FEEDS = [
    {
        "name": "pr_newswire",
        "url": "https://www.prnewswire.com/rss/technology-latest-news/technology-latest-news-list.rss",
    },
    {
        "name": "business_tech",
        "url": "https://www.prnewswire.com/rss/business-technology-latest-news/business-technology-latest-news-list.rss",
    },
    {
        "name": "consumer_tech",
        "url": "https://www.prnewswire.com/rss/consumer-technology-latest-news/consumer-technology-latest-news-list.rss",
    },
    {
        "name": "energy",
        "url": "https://www.prnewswire.com/rss/energy-latest-news/energy-latest-news-list.rss",
    },
    {
        "name": "financial",
        "url": "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    },
    {
        "name": "heavy_industry",
        "url": "https://www.prnewswire.com/rss/heavy-industry-manufacturing-latest-news/heavy-industry-manufacturing-latest-news-list.rss",
    },
    {
        "name": "telecom",
        "url": "https://www.prnewswire.com/rss/telecommunications-latest-news/telecommunications-latest-news-list.rss",
    },
]

# Google News：补抓 SaaS/Agent × 大模型合作（PRN/SEC 常漏）
GOOGLE_NEWS_QUERIES = [
    '(Anthropic OR OpenAI OR "AI agent" OR Agentforce OR Claudeforce) '
    "(partnership OR collaboration OR integration) when:3d",
    '"strategic partnership" (Anthropic OR OpenAI OR Claude) when:3d',
]

# Finnhub：无稳定 IR RSS 的标的走此通道（IR 失败时亦作备份）
FINNHUB_NEWS_TICKERS = sorted(set([
    # 大模型 / 云 / SaaS
    "CRM", "MSFT", "GOOGL", "AMZN", "META", "ORCL", "NOW", "SNOW", "ADBE", "PLTR", "IBM", "AAPL",
    # 芯片 / 半导体 / 设备
    "NVDA", "AMD", "INTC", "AVGO", "MRVL", "MU", "QCOM", "TSM", "ASML", "ARM", "SMCI", "DELL",
    "AMAT", "LRCX", "KLAC", "SNPS", "CDNS", "MCHP",
    # 数据中心 / 网络
    "EQIX", "DLR", "VRT", "ANET", "CSCO", "NTNX", "CIEN",
    # 算力租赁 / 挖矿
    "CRWV", "NBIS", "APLD", "CORZ", "WULF", "IREN", "RIOT", "MARA", "CLSK", "CIFR", "HUT", "BITF",
    # 光模块
    "LITE", "COHR",
    # 企业 AI / 安全
    "DDOG", "MDB", "PATH", "AI", "CRWD", "PANW", "FTNT", "ZS",
    # 存储
    "WDC", "STX", "PSTG", "NTAP",
]))
FINNHUB_NEWS_LOOKBACK_DAYS = 3

# IR RSS 列表见 company_ir_feeds.json（AI 产业链官网直连，并行抓取，优先于 Finnhub）
COMPANY_IR_FEEDS = _load_company_ir_feeds()

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
