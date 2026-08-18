"""PR Newswire / GlobeNewswire RSS 抓取。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from app.deal_monitor.config import PR_WIRE_FEEDS

logger = logging.getLogger(__name__)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


@dataclass
class RawItem:
    headline: str
    summary: str
    source: str
    source_url: str
    published_at: datetime


def _parse_date(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


def _text(el: ElementTree.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _parse_rss(xml: str, source: str) -> list[RawItem]:
    root = ElementTree.fromstring(xml)
    items: list[RawItem] = []
    for item in root.findall(".//item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        desc = _text(item.find("description"))
        content = _text(item.find("content:encoded", NS))
        pub = _parse_date(_text(item.find("pubDate")))
        if title and link:
            items.append(
                RawItem(
                    headline=title[:500],
                    summary=(content or desc)[:2000],
                    source=source,
                    source_url=link[:500],
                    published_at=pub,
                )
            )
    for entry in root.findall(".//atom:entry", NS):
        title = _text(entry.find("atom:title", NS))
        link_el = entry.find("atom:link", NS)
        link = link_el.get("href", "") if link_el is not None else ""
        summary = _text(entry.find("atom:summary", NS))
        content = _text(entry.find("atom:content", NS))
        updated = _parse_date(_text(entry.find("atom:updated", NS)) or _text(entry.find("atom:published", NS)))
        if title and link:
            items.append(
                RawItem(
                    headline=title[:500],
                    summary=(content or summary)[:2000],
                    source=source,
                    source_url=link[:500],
                    published_at=updated,
                )
            )
    return items


async def fetch_pr_wires() -> list[RawItem]:
    results: list[RawItem] = []
    seen_urls: set[str] = set()
    headers = {"User-Agent": "AthenaDealMonitor/1.0"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=90) as client:
        for feed in PR_WIRE_FEEDS:
            try:
                feed_url = str(feed["url"]).strip().rstrip("/")
                resp = await client.get(feed_url)
                resp.raise_for_status()
                items = _parse_rss(resp.text, feed["name"])
                kept = 0
                for item in items:
                    url = (item.source_url or "").strip().rstrip("/")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    item.source_url = url
                    results.append(item)
                    kept += 1
                logger.info("RSS %s: %d 条（去重后 %d）", feed["name"], len(items), kept)
            except Exception as exc:
                logger.warning("RSS %s 抓取失败: %r", feed["name"], exc)
    return results
