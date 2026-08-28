"""种子公司官网 / IR 新闻 RSS（优先于 Finnhub，降低索引延迟）。"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from app.deal_monitor.config import COMPANY_IR_FEEDS
from app.deal_monitor.fetchers.pr_wire import RawItem, _parse_rss

logger = logging.getLogger(__name__)

# 与 Finnhub 粗筛一致，控制进 LLM 的条数
_PREFILTER = re.compile(
    r"(anthropic|openai|claude|gpt|xai|agentforce|ai\s*agent|agentic|"
    r"large\s*language\s*model|\bllm\b|generative\s*ai|copilot|"
    r"partnership|collaboration|integration|plugin|"
    r"data\s*center|gpu|custom\s*semiconductor|hyperscale|"
    r"算力|数据中心|人工智能|strategic)",
    re.I,
)


async def fetch_company_ir_feeds() -> list[RawItem]:
    """抓取配置中的公司 IR RSS；source 形如 ir:CRM。并行抓取，单源失败不影响其它源。"""
    if not COMPANY_IR_FEEDS:
        return []

    results: list[RawItem] = []
    seen: set[str] = set()
    headers = {"User-Agent": "AthenaDealMonitor/1.0"}
    sem = asyncio.Semaphore(8)

    async def _one_feed(
        client: httpx.AsyncClient, feed: dict
    ) -> tuple[str, list[RawItem], str | None]:
        ticker = feed["ticker"]
        url = str(feed["url"]).strip()
        name = feed.get("name") or f"ir_{ticker.lower()}"
        async with sem:
            try:
                resp = await client.get(url, timeout=20)
                resp.raise_for_status()
                parsed = _parse_rss(resp.text, f"ir:{ticker}")
                kept: list[RawItem] = []
                for item in parsed:
                    blob = f"{item.headline}\n{item.summary}"
                    if not _PREFILTER.search(blob):
                        continue
                    link = (item.source_url or "").strip().rstrip("/")
                    if not link:
                        continue
                    item.source = f"ir:{ticker}"
                    item.source_url = link
                    kept.append(item)
                logger.info(
                    "IR RSS %s (%s): parsed=%d kept=%d",
                    ticker,
                    name,
                    len(parsed),
                    len(kept),
                )
                return ticker, kept, None
            except Exception as exc:
                logger.warning("IR RSS %s 失败 %s: %r", ticker, url[:60], exc)
                return ticker, [], str(exc)[:120]

    async with httpx.AsyncClient(
        headers=headers, follow_redirects=True, timeout=25
    ) as client:
        batches = await asyncio.gather(
            *[_one_feed(client, feed) for feed in COMPANY_IR_FEEDS]
        )

    for _ticker, items, _err in batches:
        for item in items:
            link = item.source_url
            if link in seen:
                continue
            seen.add(link)
            results.append(item)

    logger.info(
        "IR RSS 合计: feeds=%d ok_items=%d",
        len(COMPANY_IR_FEEDS),
        len(results),
    )
    return results
