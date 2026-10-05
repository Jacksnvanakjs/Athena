"""日度名义成交额与月度聚合。"""

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


def soxl_closes_from_bars(
    bars_by_symbol: dict[str, list[OhlcvBar]],
) -> dict[date, float]:
    """从篮子 OHLCV 抽出 SOXL 收盘价。"""
    out: dict[date, float] = {}
    for d, close, _vol in bars_by_symbol.get("SOXL") or []:
        try:
            c = float(close)
        except (TypeError, ValueError):
            continue
        if c > 0:
            out[d] = c
    return out


def soxl_closes_from_points(points: Iterable[dict]) -> dict[date, float]:
    out: dict[date, float] = {}
    for p in points:
        raw = str(p.get("date") or "")[:10]
        if not raw:
            continue
        try:
            d = date.fromisoformat(raw)
            c = float(p.get("soxl_close"))
        except (TypeError, ValueError):
            continue
        if c > 0:
            out[d] = c
    return out


def _pct_change(prev: float, curr: float) -> float | None:
    if prev is None or curr is None or prev <= 0 or curr <= 0:
        return None
    return round((curr / prev - 1.0) * 100.0, 2)


def attach_soxl_daily_returns(
    points: list[dict],
    closes: dict[date, float],
) -> list[dict]:
    """给日度点挂 SOXL 收盘与相对前一交易日涨跌幅（%）。"""
    if not points:
        return points
    by_date = dict(sorted(closes.items()))
    dates = list(by_date)
    ret_by: dict[date, float | None] = {}
    for i, d in enumerate(dates):
        if i == 0:
            ret_by[d] = None
            continue
        ret_by[d] = _pct_change(by_date[dates[i - 1]], by_date[d])
    out: list[dict] = []
    for p in points:
        row = dict(p)
        try:
            d = date.fromisoformat(str(p.get("date") or "")[:10])
        except ValueError:
            out.append(row)
            continue
        if d in by_date:
            row["soxl_close"] = round(float(by_date[d]), 4)
        if d in ret_by:
            row["soxl_ret_pct"] = ret_by[d]
        out.append(row)
    return out


def attach_soxl_monthly_returns(
    points: list[dict],
    closes: dict[date, float],
) -> list[dict]:
    """月度点：SOXL 该月末收 vs 上月末收 的涨跌幅（%）。"""
    if not points:
        return points
    last_by_month: dict[str, float] = {}
    for d, c in sorted(closes.items()):
        if c and c > 0:
            last_by_month[month_key(d)] = float(c)
    months = list(sorted(last_by_month))
    ret_by: dict[str, float | None] = {}
    for i, mk in enumerate(months):
        if i == 0:
            ret_by[mk] = None
            continue
        ret_by[mk] = _pct_change(last_by_month[months[i - 1]], last_by_month[mk])
    out: list[dict] = []
    for p in points:
        row = dict(p)
        mk = str(p.get("month") or "")
        if mk in last_by_month:
            row["soxl_close"] = round(float(last_by_month[mk]), 4)
        if mk in ret_by:
            row["soxl_ret_pct"] = ret_by[mk]
        out.append(row)
    return out


def to_daily_points(
    daily: dict[date, float],
    *,
    start_date: date | None = None,
    partial_date: date | None = None,
) -> list[dict]:
    """日度名义成交额序列（十亿美元）。

    ``partial_date``：将该日标为未完结（盘中/未收盘），供 UI 加 *。
    """
    points: list[dict] = []
    for d, usd in sorted(daily.items()):
        if start_date and d < start_date:
            continue
        usd_f = float(usd)
        if usd_f <= 0:
            continue
        points.append(
            {
                "date": d.isoformat(),
                "notional_usd": round(usd_f, 2),
                "notional_bn": round(usd_f / 1e9, 3),
                "is_partial": bool(partial_date and d == partial_date),
            }
        )
    return points


def daily_points_to_map(points: Iterable[dict]) -> dict[date, float]:
    out: dict[date, float] = {}
    for p in points:
        raw = str(p.get("date") or "")[:10]
        if not raw:
            continue
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            continue
        out[d] = float(p.get("notional_usd") or 0)
    return out


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
    return [
        p
        for p in rows
        if str(p.get("month") or p.get("date") or "").startswith(y)
    ]


def build_stats(points: list[dict]) -> dict:
    if not points:
        return {"min": None, "max": None, "latest": None}

    def pack(p: dict) -> dict:
        label = p.get("month") or p.get("date")
        return {
            "month": p.get("month"),
            "date": p.get("date"),
            "label": label,
            "notional_bn": p["notional_bn"],
            "is_partial": bool(p.get("is_partial")),
        }

    finite = [p for p in points if p.get("notional_bn") is not None]
    mn = min(finite, key=lambda p: float(p["notional_bn"]))
    mx = max(finite, key=lambda p: float(p["notional_bn"]))
    return {"min": pack(mn), "max": pack(mx), "latest": pack(points[-1])}
