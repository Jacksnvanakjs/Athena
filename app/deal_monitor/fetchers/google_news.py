"""Google News RSS：补抓 PR Newswire/SEC 覆盖不到的 AI 软件合作稿。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx

from app.deal_monitor.config import GOOGLE_NEWS_QUERIES
from app.deal_monitor.fetchers.pr_wire import RawItem

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def _text(el: ElementTree.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_rss(xml: str) -> list[RawItem]:
    root = ElementTree.fromstring(xml)
    items: list[RawItem] = []
    for item in root.findall(".//item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        desc = _text(item.find("description"))
        pub = _parse_date(_text(item.find("pubDate")))
        source_el = item.find("source")
        source_name = _text(source_el) if source_el is not None else "google_news"
        if title and link:
            items.append(
                RawItem(
                    headline=title[:500],
                    summary=desc[:2000],
                    source=f"google_news:{source_name}"[:40],
                    source_url=link[:500],
                    published_at=pub,
                )
            )
    return items


def _feed_url(query: str) -> str:
    q = quote_plus(query)
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


async def fetch_google_news(queries: list[str] | None = None) -> list[RawItem]:
    query_list = queries or GOOGLE_NEWS_QUERIES
    results: list[RawItem] = []
    seen: set[str] = set()
    headers = {"User-Agent": "AthenaDealMonitor/1.0"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=40) as client:
        for query in query_list:
            url = _feed_url(query)
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                items = _parse_rss(resp.text)
                kept = 0
                for item in items:
                    u = (item.source_url or "").strip()
                    if not u or u in seen:
                        continue
                    seen.add(u)
                    results.append(item)
                    kept += 1
                logger.info("Google News q=%r: %d 条（去重后 %d）", query[:60], len(items), kept)
            except Exception as exc:
                logger.warning("Google News 抓取失败 q=%r: %r", query[:60], exc)
    return results
