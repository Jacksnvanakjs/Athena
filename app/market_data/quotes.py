"""实时报价多源入口（复用 heatmap 级联，统一对外）。"""

from __future__ import annotations

from typing import Any


async def fetch_quotes(
    symbols: list[str],
) -> tuple[dict[str, dict[str, Any]], str]:
    """TickDB → Yahoo → AKShare → Tushare；返回 ({sym: row}, source_label)。"""
    from app.heatmap import get_quotes_for_symbols

    return await get_quotes_for_symbols(symbols)
