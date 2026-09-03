"""deal_monitor 专用配置（复用 app.config 中的 DEAL_* 环境变量）。"""

from pathlib import Path

from app.config import (
    DEAL_DEDUP_DAYS,
    DEAL_INGEST_MAX_AGE_DAYS,
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
    GEMINI_API_KEYS,
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
    loaded: list[dict[str, str]] = []
    for item in feeds:
        if not item.get("ticker"):
            continue
        feed_type = str(item.get("type") or "rss").lower()
        entry: dict[str, str] = {
            "ticker": str(item["ticker"]).upper(),
            "name": item.get("name") or f"ir_{item['ticker'].lower()}",
            "category": item.get("category") or "",
            "type": feed_type,
        }
        if feed_type == "google_news":
            query = str(item.get("query") or item.get("url") or "").strip()
            if not query:
                continue
            entry["query"] = query
        else:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            entry["url"] = url
        loaded.append(entry)
    return loaded

# 通稿 RSS：PR Newswire + Business Wire（免费行业 feed）+ GlobeNewswire
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
    # Business Wire 公开行业 RSS（feed.businesswire.com，无需付费）
    {
        "name": "business_wire",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpQWg==",
    },
    {
        "name": "business_wire_iot",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJaF1hUVA==",
    },
    {
        "name": "business_wire_ma",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRWA==",
    },
    {
        "name": "business_wire_funding",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFtRXw==",
    },
    # GlobeNewswire 公开 subject RSS（失败则忽略，由 Google site: 兜底）
    {
        "name": "globenewswire",
        "url": "https://www.globenewswire.com/RssFeed/subjectcode/13/feedTitle/GlobeNewswire%20-%20Technology",
    },
]

# Google News：补抓 SaaS/Agent × 大模型合作（PRN/SEC 常漏）
# 列表靠前 = 每轮优先查询（抢时效 when:1d/2d）
GOOGLE_NEWS_QUERIES = [
    # 通稿站直连失败时的免费兜底（BW/GNW）
    'site:businesswire.com (partnership OR collaboration OR "strategic") '
    "(AI OR Anthropic OR OpenAI OR Nvidia OR Claude OR GPU) when:2d",
    "site:globenewswire.com (partnership OR collaboration OR integration) "
    "(AI OR Anthropic OR OpenAI OR Nvidia OR Claude) when:2d",
    # 云厂/hyperscaler × 电力/地热/PPA（Fervo×Google 类一手通稿；勿只靠 AI 关键词）
    'site:globenewswire.com (PPA OR "power purchase" OR geothermal OR "data center") '
    "(Google OR Microsoft OR Amazon OR Meta OR Oracle OR Nvidia) when:3d",
    '(Google OR Microsoft OR Amazon OR Meta OR "Alphabet") '
    '(PPA OR "power purchase agreement" OR geothermal OR "carbon-free" OR offtake) '
    '("data center" OR datacenter OR hyperscale) when:2d',
    '(Fervo OR FRVO OR "Eos Energy" OR EOSE) '
    '(Google OR Microsoft OR Amazon OR PPA OR geothermal OR "data center") when:3d',
    # Adobe 无官方 RSS，用 Google News 补官网新闻室
    "site:news.adobe.com (partnership OR collaboration OR AI OR agent OR integration) when:3d",
    '(Adobe OR ADBE) (partnership OR collaboration OR "strategic") (AI OR Agent OR Claude) when:2d',
    # 大额算力 / DC 快讯（WSJ/Reuters 体，HUT 间接受益）
    '("cloud deal" OR "compute deal" OR "cloud computing deal" OR "computing deal") '
    '(Anthropic OR Lambda OR "Hut 8" OR HUT OR Nscale) when:1d',
    '("billion" OR "$35" OR "$35B") (Anthropic AND Lambda) when:2d',
    '("Hut 8" OR HUT) (Anthropic OR Lambda OR Nvidia OR Nueces OR "data center") when:3d',
    '(Anthropic OR OpenAI OR "AI agent" OR Agentforce OR Claudeforce) '
    "(partnership OR collaboration OR integration) when:3d",
    '"strategic partnership" (Anthropic OR OpenAI OR Claude) when:3d',
    '("cloud deal" OR "compute deal" OR "capacity agreement") '
    "(Anthropic OR Lambda OR Hut 8 OR CoreWeave OR Nscale) when:7d",
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
    # 云厂供电 / 地热 / 储能（AI 数据中心电力催化）
    "FRVO", "EOSE", "BE", "FLNC", "CEG", "VST", "TLN", "GEV",
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
