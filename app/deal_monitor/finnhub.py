"""Finnhub 公司搜索：公司名/片段 -> ticker。"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def search_symbol(query: str, finnhub_api_key: str) -> str | None:
    """使用 Finnhub /search?q=... 返回最可能的 ticker。

    注意：Finnhub 搜索结果不一定完全准确，因此返回值取第一条且做简单过滤。
    """

    if not query.strip():
        return None
    if not finnhub_api_key:
        return None

    url = "https://finnhub.io/api/v1/search"
    params = {"q": query, "token": finnhub_api_key}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            data: dict[str, Any] = resp.json()
    except Exception as exc:
        logger.debug("Finnhub search failed for %s: %s", query, exc)
        return None

    results = data.get("result") or []
    if not isinstance(results, list) or not results:
        return None

    scored: list[tuple[int, str]] = []
    for r in results:
        symbol = r.get("symbol")
        if not symbol:
            continue
        symbol = str(symbol).strip().upper()
        if "." in symbol or len(symbol) < 1 or len(symbol) > 7:
            continue

        score = 0
        desc = str(r.get("description") or "").lower()
        rtype = str(r.get("type") or "").lower()
        display = str(r.get("displaySymbol") or symbol).upper()

        if "common stock" in rtype or "equity" in rtype:
            score += 5
        if any(k in desc for k in ("inc", "corp", "corporation", "ltd", "company")):
            score += 2
        if query.lower() in desc:
            score += 3
        if display == symbol:
            score += 1

        scored.append((score, symbol))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]

