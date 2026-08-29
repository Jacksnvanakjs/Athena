"""加载 earnings_universe.json + 市值过滤（踢出 T0）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.deal_monitor.tiers import classify_tier
from app.earnings_monitor.config import (
    DEAL_T0_MIN_CAP,
    UNIVERSE_CANDIDATES,
)

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
    for item in rows:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
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
    """返回 (item, tier, cap)；排除 T0 / UNKNOWN。"""
    kept: list[tuple[UniverseTicker, str, float | None]] = []
    for item in items:
        cap = caps.get(item.ticker)
        tier = classify_tier(cap, ticker=item.ticker)
        if tier == "T0" or (cap is not None and cap >= DEAL_T0_MIN_CAP):
            logger.info("财报池踢出 T0: %s cap=%s", item.ticker, cap)
            continue
        if tier == "UNKNOWN" and cap is None:
            # 暂无市值：仍入库，tier 标 UNKNOWN，评分时淘汰推送
            kept.append((item, "UNKNOWN", None))
            continue
        if tier in ("T1", "T2", "UNKNOWN"):
            kept.append((item, tier if tier != "UNKNOWN" else "T2", cap))
    return kept
