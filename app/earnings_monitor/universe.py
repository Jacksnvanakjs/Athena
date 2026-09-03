"""加载 earnings_universe.json + 市值分档（全量监控入库）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.deal_monitor.tiers import classify_tier
from app.earnings_monitor.config import UNIVERSE_CANDIDATES

logger = logging.getLogger(__name__)


@dataclass
class UniverseTicker:
    ticker: str
    name: str
    sector: str


def _resolve_universe_path() -> Path | None:
    for path in UNIVERSE_CANDIDATES:
        if path.is_file():
            return path
    return None


def load_universe() -> list[UniverseTicker]:
    path = _resolve_universe_path()
    if not path:
        logger.warning("earnings_universe.json 未找到")
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("tickers") or []
    out: list[UniverseTicker] = []
    seen: set[str] = set()
    for item in rows:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(
            UniverseTicker(
                ticker=ticker,
                name=str(item.get("name") or ticker).strip(),
                sector=str(item.get("sector") or "AI_SAAS").strip().upper(),
            )
        )
    return out


def filter_by_market_cap(
    items: list[UniverseTicker],
    caps: dict[str, float | None],
) -> list[tuple[UniverseTicker, str, float | None]]:
    """全量保留进日历监控；T0/大市值只标档位，推送由评分硬淘汰。"""
    kept: list[tuple[UniverseTicker, str, float | None]] = []
    for item in items:
        cap = caps.get(item.ticker)
        tier = classify_tier(cap, ticker=item.ticker)
        if tier == "UNKNOWN" and cap is None:
            kept.append((item, "UNKNOWN", None))
            logger.info("财报池暂无市值仍监控: %s", item.ticker)
            continue
        if tier == "T0":
            logger.info("财报池保留 T0 仅日历/观察: %s cap=%s", item.ticker, cap)
        kept.append((item, tier if tier != "UNKNOWN" else "T2", cap))
    return kept
