"""加载 AI 主题篮子。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.ai_mainline.config import BASKET_CANDIDATES

logger = logging.getLogger(__name__)

_cache: dict[str, Any] | None = None


def _resolve_path() -> Path | None:
    for p in BASKET_CANDIDATES:
        if p.is_file():
            return p
    return None


def load_baskets(force: bool = False) -> dict[str, Any]:
    global _cache
    if _cache is not None and not force:
        return _cache
    path = _resolve_path()
    if not path:
        logger.error("ai_theme_baskets.json not found in %s", BASKET_CANDIDATES)
        _cache = {"version": 1, "benchmark_key": "ai_bench", "themes": []}
        return _cache
    with open(path, encoding="utf-8") as f:
        _cache = json.load(f)
    return _cache


def enabled_themes() -> list[dict[str, Any]]:
    data = load_baskets()
    return [t for t in data.get("themes", []) if t.get("enabled", True)]


def all_symbols() -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for theme in enabled_themes():
        for t in theme.get("tickers", []):
            sym = (t.get("symbol") or "").upper().strip()
            if sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
    return out


def theme_by_key(key: str) -> dict[str, Any] | None:
    for t in enabled_themes():
        if t.get("key") == key:
            return t
    return None
