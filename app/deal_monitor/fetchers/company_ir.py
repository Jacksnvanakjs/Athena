"""公司 IR / 第三方新闻聚合（Phase 2）：Finnhub 公司新闻 + Google News RSS。"""

from __future__ import annotations

from app.deal_monitor.fetchers.finnhub_news import fetch_finnhub_company_news
from app.deal_monitor.fetchers.google_news import fetch_google_news
from app.deal_monitor.fetchers.pr_wire import RawItem


async def fetch_company_ir_and_aggregators() -> list[RawItem]:
    """合并 Finnhub 与 Google News；按 URL 去重。"""
    items: list[RawItem] = []
    seen: set[str] = set()
    for batch in (await fetch_finnhub_company_news(), await fetch_google_news()):
        for item in batch:
            url = (item.source_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            items.append(item)
    return items
