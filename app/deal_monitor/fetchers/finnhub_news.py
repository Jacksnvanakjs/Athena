"""Finnhub 公司新闻：覆盖 IR/Business Wire 类合作稿（如 Salesforce×Anthropic）。"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from app.deal_monitor.config import FINNHUB_API_KEY, FINNHUB_NEWS_LOOKBACK_DAYS, FINNHUB_NEWS_TICKERS
from app.deal_monitor.fetchers.pr_wire import RawItem

logger = logging.getLogger(__name__)

# 进管线前粗筛，控制 LLM 成本；正式相关性仍由 LLM/规则决定
_PREFILTER = re.compile(
    r"(anthropic|openai|claude|gpt|xai|agentforce|ai\s*agent|agentic|"
    r"large\s*language\s*model|\bllm\b|generative\s*ai|copilot|"
    r"partnership|collaboration|integration|plugin|"
    r"data\s*center|gpu|custom\s*semiconductor|hyperscale|"
    r"算力|数据中心|人工智能)",
    re.I,
)


def _prefer_url(url: str, summary: str) -> str:
    """若 summary 本身是官网/通稿链接，优先用作 source_url。"""
    s = (summary or "").strip()
    if s.startswith("http://") or s.startswith("https://"):
        # 有的条目 summary 只有一行 URL
        first = s.split()[0].rstrip(").,]")
        if "salesforce.com" in first or "businesswire.com" in first or "prnewswire.com" in first:
            return first[:500]
    return (url or "")[:500]


async def fetch_finnhub_company_news() -> list[RawItem]:
    if not FINNHUB_API_KEY or not FINNHUB_NEWS_TICKERS:
        return []

    end = date.today()
    start = end - timedelta(days=max(1, FINNHUB_NEWS_LOOKBACK_DAYS))
    results: list[RawItem] = []
    seen: set[str] = set()

    async with httpx.AsyncClient(timeout=30) as client:
        for ticker in FINNHUB_NEWS_TICKERS:
            try:
                resp = await client.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={
                        "symbol": ticker,
                        "from": start.isoformat(),
                        "to": end.isoformat(),
                        "token": FINNHUB_API_KEY,
                    },
                )
                resp.raise_for_status()
                rows = resp.json()
                if not isinstance(rows, list):
                    logger.warning("Finnhub news %s 返回异常: %s", ticker, rows)
                    continue
                kept = 0
                for row in rows:
                    headline = (row.get("headline") or "").strip()
                    summary = (row.get("summary") or "").strip()
                    blob = f"{headline}\n{summary}"
                    if not headline or not _PREFILTER.search(blob):
                        continue
                    url = _prefer_url(row.get("url") or "", summary)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    ts = row.get("datetime")
                    if isinstance(ts, (int, float)) and ts > 0:
                        published = datetime.fromtimestamp(ts, tz=timezone.utc)
                    else:
                        published = datetime.now(timezone.utc)
                    # summary 若是纯 URL，用 headline 补一点上下文
                    body = summary if summary and not summary.startswith("http") else headline
                    results.append(
                        RawItem(
                            headline=headline[:500],
                            summary=body[:2000],
                            source=f"finnhub:{ticker}",
                            source_url=url,
                            published_at=published,
                        )
                    )
                    kept += 1
                logger.info("Finnhub news %s: raw=%d kept=%d", ticker, len(rows), kept)
            except Exception as exc:
                logger.warning("Finnhub news %s 失败: %r", ticker, exc)

    return results
