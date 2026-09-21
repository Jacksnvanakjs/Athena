"""科技杠杆 ETF：回填/日更、读写月度序列。"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from app.lev_etf.aggregate import (
    aggregate_monthly,
    build_stats,
    daily_basket_notional,
    filter_year,
    month_key,
)
from app.lev_etf.basket import (
    all_tickers,
    load_basket,
    monthly_cache_path,
    start_date_iso,
)
from app.lev_etf.fetch_ohlcv import fetch_basket_ohlcv
from app.utils import now_beijing

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_DISCLAIMER = (
    "Public market proxy (Close×Volume sum). Not Apex Fintech proprietary data. "
    "名义成交额为公开行情代理，非 Apex 专有数据。"
)

_UPDATE_LOCK = asyncio.Lock()
_MEM: dict[str, Any] = {"ts": 0.0, "payload": None}
_MEM_TTL_SEC = 300.0
_BG_STARTED = False
_BG_LOCK = threading.Lock()


def _today_et() -> date:
    return datetime.now(ET).date()


def _parse_start() -> date:
    raw = start_date_iso()
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return date(2023, 1, 1)


def _available_years(points: list[dict]) -> list[int]:
    years: set[int] = set()
    for p in points:
        mk = str(p.get("month") or "")
        if len(mk) >= 4 and mk[:4].isdigit():
            years.add(int(mk[:4]))
    return sorted(years)


def _payload_from_points(
    points: list[dict],
    *,
    year_filter: str = "all",
    as_of_date: str | None = None,
    updated_at: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    basket = load_basket()
    filtered = filter_year(points, year_filter)
    as_of = as_of_date
    if not as_of:
        for p in reversed(points):
            if p.get("as_of_date"):
                as_of = p["as_of_date"]
                break
    return {
        "success": True,
        "basket_key": basket.get("key") or "tech_lev_etf",
        "name": basket.get("name") or "科技杠杆/反向 ETF",
        "unit": "USD_billion",
        "start_month": basket.get("start_month") or "2023-01",
        "year_filter": year_filter,
        "as_of_date": as_of,
        "updated_at": updated_at or now_beijing().isoformat(timespec="seconds"),
        "disclaimer": _DISCLAIMER,
        "points": filtered,
        "stats": build_stats(filtered),
        "available_years": _available_years(points),
        "note": note,
        "ticker_count": len(all_tickers(basket)),
    }


def _read_json_cache() -> dict[str, Any] | None:
    path = monthly_cache_path()
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("points"), list):
            return data
    except Exception as exc:
        logger.warning("read lev-etf monthly cache failed: %s", exc)
    return None


def _write_json_cache(payload: dict[str, Any]) -> None:
    path = monthly_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception as exc:
        logger.warning("write lev-etf monthly cache failed: %s", exc)


def _load_points_from_db() -> list[dict] | None:
    try:
        from app.database import LevEtfMonthly, SessionLocal, is_db_ready
    except Exception:
        return None
    if not is_db_ready():
        return None
    try:
        with SessionLocal() as db:
            rows = (
                db.query(LevEtfMonthly)
                .filter(LevEtfMonthly.basket_key == "tech_lev_etf")
                .order_by(LevEtfMonthly.month)
                .all()
            )
        if not rows:
            return None
        return [
            {
                "month": r.month,
                "notional_usd": float(r.notional_usd or 0),
                "notional_bn": float(r.notional_bn or 0),
                "trading_days": int(r.trading_days or 0),
                "is_partial": bool(r.is_partial),
                "as_of_date": r.as_of_date.isoformat() if r.as_of_date else None,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("load lev-etf monthly from db failed: %s", exc)
        return None


def _upsert_points_db(points: list[dict]) -> int:
    try:
        from app.database import LevEtfMonthly, SessionLocal, is_db_ready
    except Exception:
        return 0
    if not is_db_ready() or not points:
        return 0
    n = 0
    try:
        with SessionLocal() as db:
            existing = {
                r.month: r
                for r in db.query(LevEtfMonthly)
                .filter(LevEtfMonthly.basket_key == "tech_lev_etf")
                .all()
            }
            now = now_beijing()
            for p in points:
                mk = p["month"]
                as_of = None
                if p.get("as_of_date"):
                    try:
                        as_of = date.fromisoformat(str(p["as_of_date"])[:10])
                    except ValueError:
                        as_of = None
                row = existing.get(mk)
                if row is None:
                    row = LevEtfMonthly(
                        basket_key="tech_lev_etf",
                        month=mk,
                        notional_usd=float(p["notional_usd"]),
                        notional_bn=float(p["notional_bn"]),
                        trading_days=int(p["trading_days"]),
                        is_partial=bool(p.get("is_partial")),
                        as_of_date=as_of,
                        updated_at=now,
                    )
                    db.add(row)
                else:
                    row.notional_usd = float(p["notional_usd"])
                    row.notional_bn = float(p["notional_bn"])
                    row.trading_days = int(p["trading_days"])
                    row.is_partial = bool(p.get("is_partial"))
                    row.as_of_date = as_of
                    row.updated_at = now
                n += 1
            db.commit()
    except Exception as exc:
        logger.warning("upsert lev-etf monthly failed: %s", exc)
        return 0
    return n


def _merge_points(base: list[dict], newer: list[dict]) -> list[dict]:
    by = {p["month"]: p for p in base}
    by.update({p["month"]: p for p in newer})
    return [by[k] for k in sorted(by)]


def _stored_points() -> list[dict]:
    db_points = _load_points_from_db()
    if db_points:
        return db_points
    cached = _read_json_cache()
    if cached and isinstance(cached.get("points"), list):
        return list(cached["points"])
    return []


def get_tech_meta() -> dict[str, Any]:
    basket = load_basket()
    points = _stored_points()
    return {
        "basket_key": basket.get("key"),
        "name": basket.get("name"),
        "name_en": basket.get("name_en"),
        "start_month": basket.get("start_month"),
        "currency": basket.get("currency") or "USD",
        "version": basket.get("version"),
        "groups": basket.get("groups") or [],
        "tickers": all_tickers(basket),
        "available_years": _available_years(points),
        "as_of_date": points[-1].get("as_of_date") if points else None,
        "point_count": len(points),
        "ticker_count": len(all_tickers(basket)),
        "disclaimer": _DISCLAIMER,
        "source": "yahoo_chart",
    }


def _maybe_kick_background_update() -> None:
    """无数据或当月过旧时后台补数，不堵 API。"""
    global _BG_STARTED
    points = _stored_points()
    need = False
    if not points:
        need = True
    else:
        as_of = points[-1].get("as_of_date")
        try:
            last = date.fromisoformat(str(as_of)[:10]) if as_of else None
        except ValueError:
            last = None
        if last is None or last < _today_et() - timedelta(days=5):
            need = True
    if not need:
        return
    with _BG_LOCK:
        if _BG_STARTED:
            return
        _BG_STARTED = True

    async def _run() -> None:
        global _BG_STARTED
        try:
            await run_lev_etf_update(force_full=not bool(points))
        finally:
            with _BG_LOCK:
                _BG_STARTED = False

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        with _BG_LOCK:
            _BG_STARTED = False


def get_monthly_payload(*, year: str = "all") -> dict[str, Any]:
    year_key = (year or "all").strip().lower()
    if year_key != "all":
        if not year_key.isdigit() or len(year_key) != 4:
            return {
                "success": False,
                "error": "year 支持 all 或四位年份",
                "points": [],
            }
        y = int(year_key)
        if y < 2023 or y > _today_et().year + 1:
            return {
                "success": False,
                "error": f"非法年份: {year_key}",
                "points": [],
            }

    now = time.monotonic()
    cached = _MEM.get("payload")
    if (
        cached
        and now - float(_MEM.get("ts") or 0) < _MEM_TTL_SEC
        and cached.get("_all_points") is not None
    ):
        all_points = cached["_all_points"]
        out = _payload_from_points(
            all_points,
            year_filter=year_key,
            as_of_date=cached.get("as_of_date"),
            updated_at=cached.get("updated_at"),
            note=cached.get("note"),
        )
        return out

    points = _stored_points()
    if not points:
        _maybe_kick_background_update()
        basket = load_basket()
        return {
            "success": True,
            "basket_key": basket.get("key") or "tech_lev_etf",
            "name": basket.get("name") or "科技杠杆/反向 ETF",
            "unit": "USD_billion",
            "start_month": basket.get("start_month") or "2023-01",
            "year_filter": year_key,
            "as_of_date": None,
            "updated_at": None,
            "disclaimer": _DISCLAIMER,
            "points": [],
            "stats": {"min": None, "max": None, "latest": None},
            "available_years": [],
            "note": "回填任务未完成或进行中，请稍后刷新。",
            "ticker_count": len(all_tickers(basket)),
            "loading": True,
        }

    _maybe_kick_background_update()
    payload = _payload_from_points(points, year_filter="all")
    mem = dict(payload)
    mem["_all_points"] = points
    _MEM["payload"] = mem
    _MEM["ts"] = now
    return _payload_from_points(
        points,
        year_filter=year_key,
        as_of_date=payload.get("as_of_date"),
        updated_at=payload.get("updated_at"),
    )


async def run_lev_etf_update(*, force_full: bool = False) -> dict[str, Any]:
    """回填或增量更新月度序列。"""
    async with _UPDATE_LOCK:
        basket = load_basket()
        tickers = all_tickers(basket)
        start = _parse_start()
        today = _today_et()
        existing = _stored_points()

        # 已有完整历史时只拉近 45 个自然日，重算近两月
        lookback_start = start
        if existing and not force_full:
            lookback_start = max(start, today - timedelta(days=45))

        t0 = time.perf_counter()
        bars = await fetch_basket_ohlcv(tickers, start=lookback_start, end=today)
        daily = daily_basket_notional(bars)
        start_month = str(basket.get("start_month") or "2023-01")
        fresh_points = aggregate_monthly(
            daily,
            start_month=start_month,
            as_of=max(daily) if daily else today,
            current_et_month=month_key(today),
        )

        if existing and not force_full:
            # 用新拉窗口覆盖近月；更早月份保留
            keep_before = month_key(lookback_start)
            kept = [p for p in existing if p["month"] < keep_before]
            points = _merge_points(kept, fresh_points)
        else:
            points = fresh_points

        if not points:
            return {
                "success": False,
                "error": "no bars fetched",
                "symbols_ok": len(bars),
                "symbols_total": len(tickers),
                "elapsed_sec": round(time.perf_counter() - t0, 1),
            }

        payload = _payload_from_points(points, year_filter="all")
        _write_json_cache(payload)
        db_n = _upsert_points_db(points)
        mem = dict(payload)
        mem["_all_points"] = points
        _MEM["payload"] = mem
        _MEM["ts"] = time.monotonic()

        result = {
            "success": True,
            "force_full": force_full,
            "symbols_ok": len(bars),
            "symbols_total": len(tickers),
            "months": len(points),
            "as_of_date": payload.get("as_of_date"),
            "db_upserted": db_n,
            "elapsed_sec": round(time.perf_counter() - t0, 1),
            "latest": (payload.get("stats") or {}).get("latest"),
        }
        logger.info("lev-etf update done: %s", result)
        return result
