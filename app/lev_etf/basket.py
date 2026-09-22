"""科技杠杆 ETF 篮子配置。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import BASE_DIR, DATA_DIR

_DEFAULT_BASKET = Path(__file__).resolve().parent / "tech_basket.json"
_DOC_BASKET = (
    BASE_DIR / "文档" / "科技杠杆ETF成交额监控" / "lev_etf_tech_basket.json"
)


def basket_path() -> Path:
    import os

    raw = (os.getenv("LEV_ETF_TECH_BASKET_PATH") or "").strip()
    if raw:
        return Path(raw)
    if _DEFAULT_BASKET.is_file():
        return _DEFAULT_BASKET
    return _DOC_BASKET


@lru_cache(maxsize=1)
def load_basket() -> dict[str, Any]:
    path = basket_path()
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not data.get("groups"):
        raise ValueError(f"invalid lev etf basket: {path}")
    return data


def all_tickers(basket: dict[str, Any] | None = None) -> list[str]:
    b = basket or load_basket()
    out: list[str] = []
    seen: set[str] = set()
    for group in b.get("groups") or []:
        for raw in group.get("tickers") or []:
            sym = str(raw or "").upper().strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            out.append(sym)
    return out


def monthly_cache_path() -> Path:
    return Path(DATA_DIR) / "lev_etf_tech_monthly.json"


def daily_cache_path() -> Path:
    return Path(DATA_DIR) / "lev_etf_tech_daily.json"


def start_date_iso(basket: dict[str, Any] | None = None) -> str:
    import os

    env = (os.getenv("LEV_ETF_TECH_START") or "").strip()
    if env:
        return env[:10]
    b = basket or load_basket()
    month = str(b.get("start_month") or "2023-01")
    return f"{month}-01"
