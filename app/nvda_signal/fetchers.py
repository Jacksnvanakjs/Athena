"""NVDA 相关稿源抓取。"""

from __future__ import annotations

import logging
import re

import httpx

from app.deal_monitor.fetchers.google_news import fetch_google_news
from app.deal_monitor.fetchers.pr_wire import RawItem, fetch_pr_wires
from app.deal_monitor.fetchers.sec_edgar import fetch_sec_8k
from app.nvda_signal.config import GOOGLE_NEWS_NVDA_QUERIES, NVDA_CIK, NVDA_NEWSROOM_RSS
from app.nvda_signal.keywords import has_nvda

logger = logging.getLogger(__name__)


def _filter_nvda(items: list[RawItem]) -> list[RawItem]:
    out: list[RawItem] = []
    for item in items:
        text = f"{item.headline}\n{item.summary}"
        if has_nvda(text):
            out.append(item)
    return out


async def fetch_nvidia_newsroom() -> list[RawItem]:
    items: list[RawItem] = []
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
            resp = await client.get(NVDA_NEWSROOM_RSS)
            resp.raise_for_status()
            from app.deal_monitor.fetchers.pr_wire import _parse_rss

            items = _parse_rss(resp.text, "nvidia_newsroom")
    except Exception as exc:
        logger.warning("NVIDIA newsroom RSS 失败: %s", exc)
    return items


async def fetch_nvda_sec_8k() -> list[RawItem]:
    all_items = await fetch_sec_8k()
    cik_pat = re.compile(rf"\({NVDA_CIK.lstrip('0')}\)|\({NVDA_CIK}\)", re.I)
    return [
        item for item in all_items
        if cik_pat.search(item.headline) or "nvidia" in item.headline.lower()
    ]


async def fetch_nvda_google_news() -> list[RawItem]:
    from app.deal_monitor.fetchers.google_news import fetch_google_news

    items = await fetch_google_news(GOOGLE_NEWS_NVDA_QUERIES)
    return _filter_nvda(items)


async def fetch_all_nvda_items() -> list[RawItem]:
    pr = _filter_nvda(await fetch_pr_wires())
    newsroom = await fetch_nvidia_newsroom()
    sec = await fetch_nvda_sec_8k()
    google = await fetch_nvda_google_news()

    merged: list[RawItem] = []
    seen: set[str] = set()
    for batch in (newsroom, sec, pr, google):
        for item in batch:
            url = (item.source_url or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            merged.append(item)
    return merged
