"""科技杠杆 ETF：回填/日更、读写日度与月度序列。"""

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
    daily_points_to_map,
    filter_year,
    month_key,
    to_daily_points,
)
from app.lev_etf.basket import (
    all_tickers,
    daily_cache_path,
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
_MEM_M: dict[str, Any] = {"ts": 0.0, "payload": None}
_MEM_D: dict[str, Any] = {"ts": 0.0, "payload": None}
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
        raw = str(p.get("month") or p.get("date") or "")
        if len(raw) >= 4 and raw[:4].isdigit():
            years.add(int(raw[:4]))
    return sorted(years)


def _normalize_year(year: str) -> str | dict[str, Any]:
    year_key = (year or "all").strip().lower()
    if year_key == "all":
        return year_key
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
    return year_key


def _payload_from_points(
    points: list[dict],
    *,
    granularity: str,
    year_filter: str = "all",
    as_of_date: str | None = None,
    updated_at: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    basket = load_basket()
    filtered = filter_year(points, year_filter)
    as_of = as_of_date
    if not as_of and points:
        last = points[-1]
        as_of = last.get("as_of_date") or last.get("date")
    return {
        "success": True,
        "basket_key": basket.get("key") or "tech_lev_etf",
        "name": basket.get("name") or "科技杠杆/反向 ETF",
        "unit": "USD_billion",
        "granularity": granularity,
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


def _empty_loading_payload(year_key: str, granularity: str) -> dict[str, Any]:
    basket = load_basket()
    return {
        "success": True,
        "basket_key": basket.get("key") or "tech_lev_etf",
        "name": basket.get("name") or "科技杠杆/反向 ETF",
        "unit": "USD_billion",
        "granularity": granularity,
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


def _read_json_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("points"), list):
            return data
    except Exception as exc:
        logger.warning("read lev-etf cache %s failed: %s", path.name, exc)
    return None


def _write_json_cache(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)
    except Exception as exc:
        logger.warning("write lev-etf cache %s failed: %s", path.name, exc)


def _load_monthly_from_db() -> list[dict] | None:
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


def _load_daily_from_db() -> list[dict] | None:
    try:
        from app.database import LevEtfDaily, SessionLocal, is_db_ready
    except Exception:
        return None
    if not is_db_ready():
        return None
    try:
        with SessionLocal() as db:
            rows = (
                db.query(LevEtfDaily)
                .filter(LevEtfDaily.basket_key == "tech_lev_etf")
                .order_by(LevEtfDaily.trade_date)
                .all()
            )
        if not rows:
            return None
        return [
            {
                "date": r.trade_date.isoformat(),
                "notional_usd": float(r.notional_usd or 0),
                "notional_bn": float(r.notional_bn or 0),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("load lev-etf daily from db failed: %s", exc)
        return None


def _upsert_monthly_db(points: list[dict]) -> int:
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


def _upsert_daily_db(points: list[dict]) -> int:
    try:
        from app.database import LevEtfDaily, SessionLocal, is_db_ready
    except Exception:
        return 0
    if not is_db_ready() or not points:
        return 0
    n = 0
    try:
        with SessionLocal() as db:
            existing = {
                r.trade_date: r
                for r in db.query(LevEtfDaily)
                .filter(LevEtfDaily.basket_key == "tech_lev_etf")
                .all()
            }
            now = now_beijing()
            for p in points:
                try:
                    td = date.fromisoformat(str(p["date"])[:10])
                except (KeyError, ValueError, TypeError):
                    continue
                row = existing.get(td)
                if row is None:
                    row = LevEtfDaily(
                        basket_key="tech_lev_etf",
                        trade_date=td,
                        notional_usd=float(p["notional_usd"]),
                        notional_bn=float(p["notional_bn"]),
                        updated_at=now,
                    )
                    db.add(row)
                else:
                    row.notional_usd = float(p["notional_usd"])
                    row.notional_bn = float(p["notional_bn"])
                    row.updated_at = now
                n += 1
            db.commit()
    except Exception as exc:
        logger.warning("upsert lev-etf daily failed: %s", exc)
        return 0
    return n


def _merge_by_key(base: list[dict], newer: list[dict], key: str) -> list[dict]:
    by = {p[key]: p for p in base if p.get(key)}
    by.update({p[key]: p for p in newer if p.get(key)})
    return [by[k] for k in sorted(by)]


def _stored_monthly_points() -> list[dict]:
    db_points = _load_monthly_from_db()
    if db_points:
        return db_points
    cached = _read_json_cache(monthly_cache_path())
    if cached and isinstance(cached.get("points"), list):
        return list(cached["points"])
    return []


def _stored_daily_points() -> list[dict]:
    db_points = _load_daily_from_db()
    if db_points:
        return db_points
    cached = _read_json_cache(daily_cache_path())
    if cached and isinstance(cached.get("points"), list):
        return list(cached["points"])
    return []


def get_tech_meta() -> dict[str, Any]:
    basket = load_basket()
    monthly = _stored_monthly_points()
    daily = _stored_daily_points()
    years = sorted(set(_available_years(monthly)) | set(_available_years(daily)))
    as_of = None
    if daily:
        as_of = daily[-1].get("date")
    elif monthly:
        as_of = monthly[-1].get("as_of_date")
    return {
        "basket_key": basket.get("key"),
        "name": basket.get("name"),
        "name_en": basket.get("name_en"),
        "start_month": basket.get("start_month"),
        "currency": basket.get("currency") or "USD",
        "version": basket.get("version"),
        "groups": basket.get("groups") or [],
        "tickers": all_tickers(basket),
        "available_years": years,
        "as_of_date": as_of,
        "point_count": len(monthly),
        "daily_point_count": len(daily),
        "ticker_count": len(all_tickers(basket)),
        "disclaimer": _DISCLAIMER,
        "source": "yahoo_chart",
        "granularities": ["day", "month"],
    }


def _maybe_kick_background_update(*, prefer_full: bool = False) -> None:
    """无数据或过旧时后台补数，不堵 API。"""
    global _BG_STARTED
    monthly = _stored_monthly_points()
    daily = _stored_daily_points()
    need = False
    force_full = prefer_full or (bool(monthly) and not daily)
    if not monthly and not daily:
        need = True
        force_full = True
    elif not daily:
        need = True
        force_full = True
    else:
        as_of = daily[-1].get("date") or (
            monthly[-1].get("as_of_date") if monthly else None
        )
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
            await run_lev_etf_update(force_full=force_full)
        finally:
            with _BG_LOCK:
                _BG_STARTED = False

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run())
    except RuntimeError:
        with _BG_LOCK:
            _BG_STARTED = False


def _get_series_payload(
    *,
    granularity: str,
    year: str,
    stored: list[dict],
    mem: dict[str, Any],
) -> dict[str, Any]:
    norm = _normalize_year(year)
    if isinstance(norm, dict):
        return norm
    year_key = norm

    now = time.monotonic()
    cached = mem.get("payload")
    if (
        cached
        and now - float(mem.get("ts") or 0) < _MEM_TTL_SEC
        and cached.get("_all_points") is not None
    ):
        return _payload_from_points(
            cached["_all_points"],
            granularity=granularity,
            year_filter=year_key,
            as_of_date=cached.get("as_of_date"),
            updated_at=cached.get("updated_at"),
            note=cached.get("note"),
        )

    if not stored:
        _maybe_kick_background_update(prefer_full=(granularity == "day"))
        return _empty_loading_payload(year_key, granularity)

    _maybe_kick_background_update(
        prefer_full=(granularity == "day" and len(stored) < 20)
    )
    payload = _payload_from_points(stored, granularity=granularity, year_filter="all")
    mem_payload = dict(payload)
    mem_payload["_all_points"] = stored
    mem["payload"] = mem_payload
    mem["ts"] = now
    return _payload_from_points(
        stored,
        granularity=granularity,
        year_filter=year_key,
        as_of_date=payload.get("as_of_date"),
        updated_at=payload.get("updated_at"),
    )


def get_monthly_payload(*, year: str = "all") -> dict[str, Any]:
    return _get_series_payload(
        granularity="month",
        year=year,
        stored=_stored_monthly_points(),
        mem=_MEM_M,
    )


def get_daily_payload(*, year: str = "all") -> dict[str, Any]:
    return _get_series_payload(
        granularity="day",
        year=year,
        stored=_stored_daily_points(),
        mem=_MEM_D,
    )


async def run_lev_etf_update(*, force_full: bool = False) -> dict[str, Any]:
    """回填或增量更新日度 + 月度序列。"""
    async with _UPDATE_LOCK:
        basket = load_basket()
        tickers = all_tickers(basket)
        start = _parse_start()
        today = _today_et()
        existing_daily = _stored_daily_points()

        # 无日度历史时必须全量，否则无法支持按天视图
        need_full = force_full or not existing_daily
        lookback_start = start
        if existing_daily and not need_full:
            lookback_start = max(start, today - timedelta(days=45))

        t0 = time.perf_counter()
        bars = await fetch_basket_ohlcv(tickers, start=lookback_start, end=today)
        daily_map = daily_basket_notional(bars)
        fresh_daily = to_daily_points(daily_map, start_date=start)

        if existing_daily and not need_full:
            keep_before = lookback_start.isoformat()
            kept = [p for p in existing_daily if str(p.get("date") or "") < keep_before]
            daily_points = _merge_by_key(kept, fresh_daily, "date")
        else:
            daily_points = fresh_daily

        if not daily_points:
            return {
                "success": False,
                "error": "no bars fetched",
                "symbols_ok": len(bars),
                "symbols_total": len(tickers),
                "elapsed_sec": round(time.perf_counter() - t0, 1),
            }

        full_daily_map = daily_points_to_map(daily_points)
        start_month = str(basket.get("start_month") or "2023-01")
        monthly_points = aggregate_monthly(
            full_daily_map,
            start_month=start_month,
            as_of=max(full_daily_map) if full_daily_map else today,
            current_et_month=month_key(today),
        )

        daily_payload = _payload_from_points(
            daily_points, granularity="day", year_filter="all"
        )
        monthly_payload = _payload_from_points(
            monthly_points, granularity="month", year_filter="all"
        )
        _write_json_cache(daily_cache_path(), daily_payload)
        _write_json_cache(monthly_cache_path(), monthly_payload)
        db_daily_n = _upsert_daily_db(daily_points)
        db_monthly_n = _upsert_monthly_db(monthly_points)

        mem_d = dict(daily_payload)
        mem_d["_all_points"] = daily_points
        _MEM_D["payload"] = mem_d
        _MEM_D["ts"] = time.monotonic()

        mem_m = dict(monthly_payload)
        mem_m["_all_points"] = monthly_points
        _MEM_M["payload"] = mem_m
        _MEM_M["ts"] = time.monotonic()

        result = {
            "success": True,
            "force_full": need_full,
            "symbols_ok": len(bars),
            "symbols_total": len(tickers),
            "days": len(daily_points),
            "months": len(monthly_points),
            "as_of_date": daily_payload.get("as_of_date"),
            "db_upserted_daily": db_daily_n,
            "db_upserted": db_monthly_n,
            "elapsed_sec": round(time.perf_counter() - t0, 1),
            "latest": (daily_payload.get("stats") or {}).get("latest"),
        }
        logger.info("lev-etf update done: %s", result)
        return result
