"""SEC EDGAR 8-K 抓取（聚焦 Item 1.01 材料性 definitive agreement）。"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from app.deal_monitor.config import SEC_USER_AGENT
from app.deal_monitor.entity_resolver import parse_sec_filer
from app.deal_monitor.fetchers.pr_wire import RawItem

logger = logging.getLogger(__name__)

SEC_8K_FEED_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K"
    "&count=100&owner=exclude&output=atom"
)

ITEM_101_MARKERS = (
    "item 1.01",
    "entry into a material definitive agreement",
    "material definitive agreement",
)

NS = {"atom": "http://www.w3.org/2005/Atom"}


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


def _parse_atom(xml: str) -> list[RawItem]:
    root = ElementTree.fromstring(xml)
    items: list[RawItem] = []
    for entry in root.findall("atom:entry", NS):
        title = _text(entry.find("atom:title", NS))
        summary = _text(entry.find("atom:summary", NS))
        updated = _text(entry.find("atom:updated", NS))
        link_el = entry.find("atom:link", NS)
        source_url = link_el.get("href", "") if link_el is not None else ""
        if title and source_url:
            items.append(
                RawItem(
                    headline=title[:500],
                    summary=summary[:2000],
                    source="sec_8k",
                    source_url=source_url[:500],
                    published_at=_parse_date(updated),
                )
            )
    return items


def _index_url(source_url: str) -> str:
    if source_url.endswith("-index.htm"):
        return source_url
    if source_url.endswith(".htm"):
        return re.sub(r"\.htm$", "-index.htm", source_url)
    return source_url


async def _fetch_index_text(client: httpx.AsyncClient, source_url: str) -> str:
    url = _index_url(source_url)
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text[:120_000]


def _has_item_101(html: str) -> bool:
    lower = html.lower()
    return any(marker in lower for marker in ITEM_101_MARKERS)


def _extract_item_101_snippet(html: str, max_len: int = 800) -> str:
    lower = html.lower()
    for marker in ITEM_101_MARKERS:
        idx = lower.find(marker)
        if idx >= 0:
            start = max(0, idx - 120)
            snippet = re.sub(r"\s+", " ", html[start : start + max_len])
            return snippet.strip()
    return ""


async def _enrich_item_101(
    client: httpx.AsyncClient,
    item: RawItem,
    sem: asyncio.Semaphore,
) -> RawItem | None:
    async with sem:
        try:
            html = await _fetch_index_text(client, item.source_url)
        except Exception as exc:
            logger.debug("SEC index 抓取失败 %s: %r", item.source_url[:80], exc)
            return None

        if not _has_item_101(html):
            return None

        filer = parse_sec_filer(item.headline) or "Unknown filer"
        snippet = _extract_item_101_snippet(html)
        prefix = f"[SEC Item 1.01] Filer: {filer}\n"
        body = snippet or item.summary or item.headline
        item.summary = (prefix + body)[:2000]
        return item


async def fetch_sec_8k(max_check: int = 80) -> list[RawItem]:
    """抓取近期 8-K，仅保留含 Item 1.01 的材料性协议申报。"""
    if not SEC_USER_AGENT:
        logger.warning("SEC_USER_AGENT 未配置，跳过 8-K")
        return []

    headers = {
        "User-Agent": SEC_USER_AGENT,
        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            resp = await client.get(SEC_8K_FEED_URL)
            resp.raise_for_status()
            raw_items = _parse_atom(resp.text)
            if not raw_items:
                return []

            sem = asyncio.Semaphore(5)
            tasks = [
                _enrich_item_101(client, item, sem)
                for item in raw_items[:max_check]
            ]
            results = await asyncio.gather(*tasks)
            kept = [r for r in results if r is not None]
            logger.info("SEC 8-K: %d 条候选, %d 条 Item 1.01", len(raw_items), len(kept))
            return kept
    except Exception as exc:
        logger.warning("SEC 8-K 抓取失败: %r", exc)
        return []
