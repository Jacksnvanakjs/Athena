"""日度 → 月度名义成交额聚合。"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Iterable

from app.lev_etf.fetch_ohlcv import OhlcvBar


def daily_basket_notional(
    bars_by_symbol: dict[str, list[OhlcvBar]],
) -> dict[date, float]:
    """各标的 Close×Volume 按日加总。"""
    by_day: dict[date, float] = {}
    for rows in bars_by_symbol.values():
        for d, close, volume in rows:
            if close is None or volume is None:
                continue
            notional = float(close) * float(volume)
            if notional <= 0:
                continue
            by_day[d] = by_day.get(d, 0.0) + notional
    return dict(sorted(by_day.items()))


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def aggregate_monthly(
    daily: dict[date, float],
    *,
    start_month: str = "2023-01",
    as_of: date | None = None,
    current_et_month: str | None = None,
) -> list[dict]:
    """聚合为月度点；当月未完 is_partial=true。"""
    if not daily:
        return []
    as_of = as_of or max(daily)
    cur_month = current_et_month or month_key(as_of)

    buckets: dict[str, dict] = {}
    for d, usd in daily.items():
        mk = month_key(d)
        if mk < start_month:
            continue
        row = buckets.get(mk)
        if row is None:
            row = {
                "month": mk,
                "notional_usd": 0.0,
                "trading_days": 0,
                "as_of_date": d,
            }
            buckets[mk] = row
        row["notional_usd"] += float(usd)
        row["trading_days"] += 1
        if d > row["as_of_date"]:
            row["as_of_date"] = d

    points: list[dict] = []
    for mk in sorted(buckets):
        row = buckets[mk]
        y, m = int(mk[:4]), int(mk[5:7])
        end = month_end(y, m)
        last = row["as_of_date"]
        is_partial = mk == cur_month and last < end
        points.append(
            {
                "month": mk,
                "notional_usd": round(float(row["notional_usd"]), 2),
                "notional_bn": round(float(row["notional_usd"]) / 1e9, 3),
                "trading_days": int(row["trading_days"]),
                "is_partial": bool(is_partial),
                "as_of_date": last.isoformat(),
            }
        )
    return points


def filter_year(points: Iterable[dict], year: str | int | None) -> list[dict]:
    rows = list(points)
    if year is None or str(year).lower() in {"", "all"}:
        return rows
    y = str(year).strip()
    return [p for p in rows if str(p.get("month") or "").startswith(y)]


def build_stats(points: list[dict]) -> dict:
    if not points:
        return {"min": None, "max": None, "latest": None}

    def pack(p: dict) -> dict:
        return {
            "month": p["month"],
            "notional_bn": p["notional_bn"],
            "is_partial": bool(p.get("is_partial")),
        }

    finite = [p for p in points if p.get("notional_bn") is not None]
    mn = min(finite, key=lambda p: float(p["notional_bn"]))
    mx = max(finite, key=lambda p: float(p["notional_bn"]))
    return {"min": pack(mn), "max": pack(mx), "latest": pack(points[-1])}
