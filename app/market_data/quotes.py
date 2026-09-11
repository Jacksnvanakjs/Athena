"""实时报价多源入口（复用 heatmap 级联，统一对外）。"""

from __future__ import annotations

from typing import Any


async def fetch_quotes(
    symbols: list[str],
    *,
    allow_slow_fill: bool = True,
) -> tuple[dict[str, dict[str, Any]], str]:
    """东财∥Finnhub → 轮动补缺(TV/Finviz/AV) → TickDB → Yahoo。"""
    from app.heatmap import get_quotes_for_symbols

    return await get_quotes_for_symbols(symbols, allow_slow_fill=allow_slow_fill)
