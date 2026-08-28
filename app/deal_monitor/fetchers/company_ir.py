"""公司 IR / 第三方新闻聚合：IR RSS（优先）+ Finnhub + Google News。"""

from __future__ import annotations

from app.deal_monitor.fetchers.company_ir_rss import fetch_company_ir_feeds
from app.deal_monitor.fetchers.finnhub_news import fetch_finnhub_company_news
from app.deal_monitor.fetchers.google_news import fetch_google_news
from app.deal_monitor.fetchers.pr_wire import RawItem


async def fetch_finnhub_and_google() -> list[RawItem]:
    """Finnhub 公司新闻 + Google News（不含 IR RSS）。"""
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


async def fetch_company_ir_and_aggregators() -> list[RawItem]:
    """IR RSS 优先，再 Finnhub / Google；按 URL 去重保留先出现的源。"""
    items: list[RawItem] = []
    seen: set[str] = set()
    for batch in (
        await fetch_company_ir_feeds(),
        await fetch_finnhub_company_news(),
        await fetch_google_news(),
    ):
        for item in batch:
            url = (item.source_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            items.append(item)
    return items
