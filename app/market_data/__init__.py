"""全站行情/基本面多源自动切换。

约定：任一数据能力至少 2 个独立源；按顺序尝试，首个有效结果即用，并打日志标明来源。
调用方应优先用本包公开 API，避免直接绑死单一 vendor。

已覆盖能力与默认链路：
- 实时报价 ``fetch_quotes``：TickDB → Yahoo → AKShare → Tushare（见 heatmap）
- 日线收盘 ``fetch_daily_closes``：Yahoo → AKShare(新浪) → Stooq
- 市值 ``fetch_market_cap``：Finnhub → Yahoo → 日线×股本（deal_monitor）
- 财报日历：Finnhub → Nasdaq → Yahoo（earnings_monitor）
- 盘后现价：报价多源 → CNBC → Yahoo AH → Finnhub（outcome）
"""

from __future__ import annotations

from app.market_data.cascade import SourceResult, first_success
from app.market_data.daily_closes import fetch_daily_closes
from app.market_data.quotes import fetch_quotes

__all__ = [
    "SourceResult",
    "first_success",
    "fetch_daily_closes",
    "fetch_quotes",
]
