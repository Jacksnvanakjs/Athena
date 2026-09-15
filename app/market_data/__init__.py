"""全站行情/基本面多源自动切换。

约定：任一数据能力至少 2 个独立源；按顺序尝试，首个有效结果即用，并打日志标明来源。
频繁测试时默认轮动起点，避免总打同一个源。

已覆盖能力与默认链路：
- 实时报价 ``fetch_quotes``：东财∥Finnhub → 轮动(TradingView/Finviz/AV) → Yahoo
- 日线收盘 ``fetch_daily_closes``：轮动(Nasdaq/东财/AV/Twelve/Tiingo/Polygon/Marketstack/Yahoo/Stooq)
- 市值 ``fetch_market_cap``：Finnhub → Yahoo → 日线×股本（deal_monitor）
- 财报日历：Finnhub → Nasdaq → Yahoo（earnings_monitor）
- 盘后现价：报价多源 → CNBC → Yahoo AH → Finnhub（outcome）
"""

from __future__ import annotations

from app.market_data.cascade import SourceResult, first_success
from app.market_data.daily_closes import (
    fetch_daily_bars,
    fetch_daily_closes,
    fetch_daily_closes_many,
)
from app.market_data.quotes import fetch_quotes
from app.market_data.tradability import is_us_tradable

__all__ = [
    "SourceResult",
    "first_success",
    "fetch_daily_closes",
    "fetch_daily_closes_many",
    "fetch_daily_bars",
    "fetch_quotes",
    "is_us_tradable",
]
