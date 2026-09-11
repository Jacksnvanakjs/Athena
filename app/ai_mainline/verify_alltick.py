"""AllTick 对照检测：不进轮动，只用来核对系统展示行情是否异常。"""

from __future__ import annotations

import logging
from typing import Any

from app.ai_mainline.baskets import all_symbols, enabled_themes
from app.ai_mainline.metrics import theme_metrics
from app.config import ALLTICK_TOKEN
from app.heatmap import _period_ret_from_closes
from app.market_data.alt_sources import (
    fetch_alltick_daily_closes,
    fetch_alltick_quotes,
)
from app.market_data.quotes import fetch_quotes

logger = logging.getLogger(__name__)


def _pct_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    if b == 0:
        return None
    return round((float(a) - float(b)) / abs(float(b)) * 100, 2)


def _split_alltick_payload(
    raw: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    meta = dict(raw.get("_meta") or {})
    quotes = {k: v for k, v in raw.items() if k != "_meta" and isinstance(v, dict)}
    return quotes, meta


async def verify_mainline_vs_alltick(
    *,
    price_tol_pct: float = 1.0,
    chg_tol_pp: float = 0.8,
    sample_period: int = 8,
) -> dict[str, Any]:
    """用 AllTick 对照系统主线篮子报价（及抽样 5D）。

    返回 mismatches / summary，不改页面数据、不写入轮动。
    """
    token = (ALLTICK_TOKEN or "").strip()
    if not token:
        return {
            "success": False,
            "ok": False,
            "verdict": "no_alltick_token",
            "error": "未配置 ALLTICK_TOKEN（仅对照用，不进轮动）",
            "mismatches": [],
        }

    symbols = all_symbols()
    sys_quotes, sys_source = await fetch_quotes(symbols, allow_slow_fill=False)
    raw_at = await fetch_alltick_quotes(symbols)
    at_quotes, at_meta = _split_alltick_payload(raw_at)

    # AllTick 完全无数据：无法对照，不能说「系统错了」
    if not at_quotes:
        hint = (
            "AllTick 未返回任何美股报价，无法对照。"
            "常见原因：套餐无美股权限（ret=604 code unauthorized）、"
            "限流（429）、或 Token 无效。"
            "系统侧已有行情时，请以 Finnhub/TradingView 等展示源为准，"
            "或升级含 All US Stocks 的 AllTick 套餐后再测。"
        )
        return {
            "success": True,
            "ok": False,
            "verdict": "alltick_unavailable",
            "comparable": False,
            "note": hint,
            "system_source": sys_source,
            "alltick_meta": at_meta,
            "counts": {
                "symbols": len(symbols),
                "system_quotes": len(sys_quotes),
                "alltick_quotes": 0,
                "compared": 0,
                "missing_system": sum(1 for s in symbols if s not in sys_quotes),
                "missing_alltick": len(symbols),
                "quote_mismatches": 0,
                "period_mismatches": 0,
                "theme_ret1d_diffs": 0,
            },
            "quote_mismatches": [],
            "period_mismatches": [],
            "theme_ret1d_diffs": [],
            "how_to_read": {
                "system": "当前页面展示源（本轮：" + str(sys_source) + "）",
                "alltick": "对照源本次无数据，不能用来判对错",
                "next": "等 AllTick 能拉到美股后，再看 quote_mismatches；有差异才需要核对谁更准",
            },
        }

    mismatches: list[dict[str, Any]] = []
    compared = 0
    missing_sys = 0
    missing_at = 0

    for sym in symbols:
        sq = sys_quotes.get(sym)
        aq = at_quotes.get(sym)
        if not sq:
            missing_sys += 1
        if not aq:
            missing_at += 1
        if not sq or not aq:
            continue
        compared += 1
        sp = float(sq.get("price") or 0)
        ap = float(aq.get("price") or 0)
        sc = sq.get("change_pct")
        ac = aq.get("change_pct")
        price_pct = _pct_diff(sp, ap)
        chg_pp = None
        if sc is not None and ac is not None:
            chg_pp = round(float(sc) - float(ac), 2)
        bad_price = price_pct is not None and abs(price_pct) > price_tol_pct
        bad_chg = chg_pp is not None and abs(chg_pp) > chg_tol_pp
        if bad_price or bad_chg:
            mismatches.append(
                {
                    "symbol": sym,
                    "kind": "quote",
                    "sys_price": sp,
                    "alltick_price": ap,
                    "price_diff_pct": price_pct,
                    "sys_change_pct": sc,
                    "alltick_change_pct": ac,
                    "change_diff_pp": chg_pp,
                }
            )

    period_mismatches: list[dict[str, Any]] = []
    sample = [s for s in symbols if s in sys_quotes and s in at_quotes][
        : max(0, sample_period)
    ]
    from app.heatmap import fetch_period_returns

    period = await fetch_period_returns(sample) if sample else {}
    for sym in sample:
        rows = await fetch_alltick_daily_closes(sym, lookback_days=40)
        closes = [c for _, c in rows]
        at_5 = _period_ret_from_closes(closes, 5)
        sys_5 = (period.get(sym) or {}).get("ret_5d")
        if at_5 is None or sys_5 is None:
            continue
        diff = round(float(sys_5) - float(at_5), 2)
        if abs(diff) > 1.5:
            period_mismatches.append(
                {
                    "symbol": sym,
                    "kind": "ret_5d",
                    "sys_ret_5d": sys_5,
                    "alltick_ret_5d": at_5,
                    "diff_pp": diff,
                }
            )

    themes_cfg = enabled_themes()
    empty_period = {s: {"ret_5d": None, "ret_20d": None} for s in symbols}
    theme_diffs: list[dict[str, Any]] = []
    for t in themes_cfg:
        a = theme_metrics(t, sys_quotes, empty_period)
        b = theme_metrics(t, at_quotes, empty_period)
        ra, rb = a.get("ret_1d"), b.get("ret_1d")
        if ra is None or rb is None:
            continue
        d = round(float(ra) - float(rb), 2)
        if abs(d) > 1.0:
            theme_diffs.append(
                {
                    "theme": t.get("name") or t.get("key"),
                    "sys_ret_1d": ra,
                    "alltick_ret_1d": rb,
                    "diff_pp": d,
                    "sys_n_valid": a.get("n_valid"),
                    "alltick_n_valid": b.get("n_valid"),
                }
            )

    ok = (
        compared > 0
        and len(mismatches) == 0
        and len(period_mismatches) == 0
        and len(theme_diffs) == 0
    )
    verdict = "match" if ok else "mismatch"
    return {
        "success": True,
        "ok": ok,
        "verdict": verdict,
        "comparable": True,
        "note": "AllTick 仅对照，不参与轮动展示",
        "system_source": sys_source,
        "alltick_meta": at_meta,
        "counts": {
            "symbols": len(symbols),
            "system_quotes": len(sys_quotes),
            "alltick_quotes": len(at_quotes),
            "compared": compared,
            "missing_system": missing_sys,
            "missing_alltick": missing_at,
            "quote_mismatches": len(mismatches),
            "period_mismatches": len(period_mismatches),
            "theme_ret1d_diffs": len(theme_diffs),
        },
        "thresholds": {
            "price_tol_pct": price_tol_pct,
            "chg_tol_pp": chg_tol_pp,
        },
        "quote_mismatches": mismatches[:40],
        "period_mismatches": period_mismatches,
        "theme_ret1d_diffs": theme_diffs[:20],
        "how_to_read": {
            "match": "两边接近 → 系统展示可信",
            "mismatch": "有差异 → 看样例字段；现价差以交易所口径核对，1D 差可能来自昨收时点不同",
            "prefer_system_if": "AllTick 套餐无美股/限流时，以系统多源（Finnhub/TV 等）为准",
        },
    }
