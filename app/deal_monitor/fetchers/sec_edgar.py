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


def _absolute_sec_url(href: str) -> str:
    href = href.strip()
    if href.startswith("http"):
        return href
    # ix viewer → 直接取 Archives 原文
    m = re.search(r"/Archives/edgar/data/\d+/\d+/[^\"'\s>]+\.htm", href, re.I)
    if m:
        return "https://www.sec.gov" + m.group(0)
    if href.startswith("/"):
        return "https://www.sec.gov" + href
    return href


def _normalize_sec_text(html: str) -> str:
    """去掉标签/实体，便于匹配 Item 1.01（SEC 常用 Item&#8201;1.01）。"""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&#8201;", " ")
        .replace("&#160;", " ")
        .replace("&nbsp;", " ")
        .replace("&#8220;", '"')
        .replace("&#8221;", '"')
        .replace("&#8217;", "'")
        .replace("&amp;", "&")
    )
    text = re.sub(r"\s+", " ", text)
    return text.strip()


async def _fetch_text(client: httpx.AsyncClient, url: str, limit: int = 200_000) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.text[:limit]


def _has_item_101(html: str) -> bool:
    lower = _normalize_sec_text(html).lower()
    return any(marker in lower for marker in ITEM_101_MARKERS)


def _find_8k_document_url(index_html: str, index_url: str) -> str | None:
    """从 index 页找到主 8-K 文档（排除 exhibit）。"""
    candidates: list[str] = []
    for m in re.finditer(r'href="([^"]+\.htm)"', index_html, re.I):
        href = m.group(1)
        abs_url = _absolute_sec_url(href)
        lower = abs_url.lower()
        if "/archives/edgar/data/" not in lower:
            continue
        if "index.htm" in lower:
            continue
        # 排除常见 exhibit / cover
        if re.search(r"ex[-_]?\d|exhibit|ex99|r\d+\.htm", lower):
            continue
        if re.search(r"d\d+d8k\.htm|form8[-_]?k|/8-?k[^a-z]", lower) or lower.endswith("8k.htm"):
            candidates.insert(0, abs_url)
        else:
            candidates.append(abs_url)
    if candidates:
        return candidates[0]
    # fallback：同目录下猜测
    base = re.sub(r"/[^/]+-index\.htm$", "/", index_url)
    return None if not base.startswith("http") else None


def _extract_item_101_snippet(html: str, max_len: int = 1200) -> str:
    text = _normalize_sec_text(html)
    lower = text.lower()
    # 优先真正的 Item 1.01 标题段，避开 “incorporated by reference into Item …”
    patterns = (
        r"item\s*1\.01\s+entry into a material definitive agreement",
        r"item\s*1\.01\s*[:.\-–]?",
        r"entry into a material definitive agreement",
    )
    idx = -1
    for pat in patterns:
        for m in re.finditer(pat, lower):
            window = lower[m.start() : m.start() + 80]
            if "incorporated by reference" in window:
                continue
            idx = m.start()
            break
        if idx >= 0:
            break
    if idx < 0:
        return ""
    # 截到下一个主要 Item 或签名前
    end = len(text)
    for m in re.finditer(r"\bitem\s*\d+\.\d+\b", lower[idx + 20 :], re.I):
        end = idx + 20 + m.start()
        break
    sig = lower.find("signature", idx + 50)
    if sig > 0:
        end = min(end, sig)
    snippet = text[idx:end].strip()
    return snippet[:max_len]


async def _enrich_item_101(
    client: httpx.AsyncClient,
    item: RawItem,
    sem: asyncio.Semaphore,
) -> RawItem | None:
    async with sem:
        try:
            index_url = _index_url(item.source_url)
            index_html = await _fetch_text(client, index_url)
        except Exception as exc:
            logger.debug("SEC index 抓取失败 %s: %r", item.source_url[:80], exc)
            return None

        if not _has_item_101(index_html):
            return None

        filer = parse_sec_filer(item.headline) or "Unknown filer"
        snippet = ""
        doc_url = _find_8k_document_url(index_html, index_url)
        if doc_url:
            try:
                doc_html = await _fetch_text(client, doc_url)
                snippet = _extract_item_101_snippet(doc_html)
            except Exception as exc:
                logger.debug("SEC 8-K 正文抓取失败 %s: %r", doc_url[:80], exc)

        # 正文失败时退回 index（通常只有 Items 列表，信息不足）
        if not snippet:
            snippet = _extract_item_101_snippet(index_html)

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
