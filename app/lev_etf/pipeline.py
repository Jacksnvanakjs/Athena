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
    attach_soxl_daily_returns,
    attach_soxl_monthly_returns,
    build_stats,
    daily_basket_notional,
    daily_points_to_map,
    filter_year,
    month_key,
    soxl_closes_from_bars,
    soxl_closes_from_points,
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
        "note": "正在自动回填历史成交额，请稍候自动刷新…",
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
                "soxl_close": (
                    float(r.soxl_close) if getattr(r, "soxl_close", None) is not None else None
                ),
                "soxl_ret_pct": (
                    float(r.soxl_ret_pct) if getattr(r, "soxl_ret_pct", None) is not None else None
                ),
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
                "soxl_close": (
                    float(r.soxl_close) if getattr(r, "soxl_close", None) is not None else None
                ),
                "soxl_ret_pct": (
                    float(r.soxl_ret_pct) if getattr(r, "soxl_ret_pct", None) is not None else None
                ),
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
                        soxl_close=p.get("soxl_close"),
                        soxl_ret_pct=p.get("soxl_ret_pct"),
                        updated_at=now,
                    )
                    db.add(row)
                else:
                    row.notional_usd = float(p["notional_usd"])
                    row.notional_bn = float(p["notional_bn"])
                    row.trading_days = int(p["trading_days"])
                    row.is_partial = bool(p.get("is_partial"))
                    row.as_of_date = as_of
                    row.soxl_close = p.get("soxl_close")
                    row.soxl_ret_pct = p.get("soxl_ret_pct")
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
                        soxl_close=p.get("soxl_close"),
                        soxl_ret_pct=p.get("soxl_ret_pct"),
                        updated_at=now,
                    )
                    db.add(row)
                else:
                    row.notional_usd = float(p["notional_usd"])
                    row.notional_bn = float(p["notional_bn"])
                    row.soxl_close = p.get("soxl_close")
                    row.soxl_ret_pct = p.get("soxl_ret_pct")
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


def _merge_soxl_fields(base: list[dict], extra: list[dict], key: str) -> list[dict]:
    """把缓存里的 SOXL 涨跌补到库内成交额点上（不覆盖已有）。"""
    by = {p[key]: p for p in extra if p.get(key)}
    out: list[dict] = []
    for p in base:
        row = dict(p)
        src = by.get(row.get(key)) or {}
        if row.get("soxl_ret_pct") is None and src.get("soxl_ret_pct") is not None:
            row["soxl_ret_pct"] = src.get("soxl_ret_pct")
            if src.get("soxl_close") is not None:
                row["soxl_close"] = src.get("soxl_close")
        elif row.get("soxl_close") is None and src.get("soxl_close") is not None:
            row["soxl_close"] = src.get("soxl_close")
        out.append(row)
    return out


def _soxl_coverage_ok(points: list[dict], *, tail: int = 24) -> bool:
    sample = points[-tail:] if points else []
    if not sample:
        return False
    hit = sum(1 for p in sample if p.get("soxl_ret_pct") is not None)
    return hit >= max(3, (len(sample) + 1) // 2)


def _run_async(coro):
    """在同步 API 线程里跑一段协程。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    holder: dict[str, Any] = {}

    def _worker() -> None:
        try:
            holder["value"] = asyncio.run(coro)
        except Exception as exc:
            holder["error"] = exc

    th = threading.Thread(target=_worker, name="lev-soxl-fetch", daemon=True)
    th.start()
    th.join(timeout=28.0)
    if "error" in holder:
        raise holder["error"]
    if "value" not in holder:
        raise TimeoutError("SOXL 拉取超时")
    return holder["value"]


async def _fetch_soxl_closes(start: date, end: date) -> dict[date, float]:
    """SOXL 收盘价：东财日 K 优先（Yahoo 易 429），再走日线多源 / OHLCV。"""
    out: dict[date, float] = {}
    lookback = max(60, (end - start).days + 20)
    pad = start - timedelta(days=14)

    def _absorb(rows: list) -> None:
        for d, c in rows or []:
            try:
                fv = float(c)
            except (TypeError, ValueError):
                continue
            if fv > 0 and pad <= d <= end:
                out[d] = fv

    try:
        from app.market_data.daily_closes import _from_eastmoney

        _absorb(await asyncio.wait_for(_from_eastmoney("SOXL", lookback), timeout=14))
    except Exception as exc:
        logger.warning("SOXL eastmoney failed: %s", exc)
    if len(out) >= 3:
        return out
    return out


_SOXL_HYDRATE_LOCK = threading.Lock()
_SOXL_HYDRATE_TS = 0.0
_SOXL_BG_LOCK = threading.Lock()
_SOXL_BG_STARTED = False


def _hydrate_soxl_returns(
    daily_points: list[dict],
    monthly_points: list[dict] | None = None,
    *,
    persist: bool = True,
) -> tuple[list[dict], list[dict] | None]:
    """缺 SOXL 涨跌时单独拉 SOXL 日线（不重拉整篮），挂到日/月点上。"""
    global _SOXL_HYDRATE_TS
    with _SOXL_HYDRATE_LOCK:
        daily = [dict(p) for p in daily_points]
        monthly = [dict(p) for p in monthly_points] if monthly_points is not None else None
        cached = _read_json_cache(daily_cache_path())
        if cached and isinstance(cached.get("points"), list):
            daily = _merge_soxl_fields(daily, cached["points"], "date")
        if monthly is not None:
            mcache = _read_json_cache(monthly_cache_path())
            if mcache and isinstance(mcache.get("points"), list):
                monthly = _merge_soxl_fields(monthly, mcache["points"], "month")
        closes = soxl_closes_from_points(daily)
        need_fetch = not _soxl_coverage_ok(daily)
        if need_fetch and daily:
            now = time.time()
            if now - _SOXL_HYDRATE_TS >= 8:
                try:
                    first = date.fromisoformat(str(daily[0].get("date") or "")[:10])
                    last = date.fromisoformat(str(daily[-1].get("date") or "")[:10])
                    fetch_start = first - timedelta(days=10)
                    fetched = _run_async(_fetch_soxl_closes(fetch_start, last))
                    if fetched:
                        closes.update(fetched)
                        _SOXL_HYDRATE_TS = now
                except Exception as exc:
                    logger.warning("hydrate SOXL closes failed: %s", exc)
        if closes:
            daily = attach_soxl_daily_returns(daily, closes)
            if monthly is not None:
                monthly = attach_soxl_monthly_returns(monthly, closes)
            if persist and _soxl_coverage_ok(daily):
                try:
                    daily_payload = _payload_from_points(
                        daily, granularity="day", year_filter="all"
                    )
                    _write_json_cache(daily_cache_path(), daily_payload)
                    _upsert_daily_db(daily)
                    if monthly is not None:
                        monthly_payload = _payload_from_points(
                            monthly, granularity="month", year_filter="all"
                        )
                        _write_json_cache(monthly_cache_path(), monthly_payload)
                        _upsert_monthly_db(monthly)
                except Exception as exc:
                    logger.warning("persist SOXL overlay failed: %s", exc)
        return daily, monthly


_SOXL_BG_STARTED = False


def _kick_soxl_hydrate_bg(daily: list[dict], monthly: list[dict] | None) -> None:
    """SOXL 着色不挡成交额接口。"""
    global _SOXL_BG_STARTED
    if not daily or _soxl_coverage_ok(daily):
        return
    with _SOXL_BG_LOCK:
        if _SOXL_BG_STARTED:
            return
        _SOXL_BG_STARTED = True

    def _run() -> None:
        global _SOXL_BG_STARTED
        try:
            _hydrate_soxl_returns(daily, monthly, persist=True)
        except Exception:
            logger.exception("SOXL hydrate background failed")
        finally:
            with _SOXL_BG_LOCK:
                _SOXL_BG_STARTED = False

    threading.Thread(target=_run, name="lev-soxl-hydrate", daemon=True).start()


def _stored_monthly_points() -> list[dict]:
    db_points = _load_monthly_from_db()
    cached = _read_json_cache(monthly_cache_path())
    cache_pts = (
        list(cached["points"])
        if cached and isinstance(cached.get("points"), list)
        else []
    )
    if db_points:
        return _merge_soxl_fields(db_points, cache_pts, "month") if cache_pts else db_points
    return cache_pts


def _stored_daily_points() -> list[dict]:
    db_points = _load_daily_from_db()
    cached = _read_json_cache(daily_cache_path())
    cache_pts = (
        list(cached["points"])
        if cached and isinstance(cached.get("points"), list)
        else []
    )
    if db_points:
        return _merge_soxl_fields(db_points, cache_pts, "date") if cache_pts else db_points
    return cache_pts


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


def _daily_last_date(points: list[dict] | None) -> date | None:
    if not points:
        return None
    raw = (points[-1] or {}).get("date") or (points[-1] or {}).get("as_of_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _needs_lev_update(daily: list[dict] | None, monthly: list[dict] | None) -> tuple[bool, bool]:
    """是否需要更新、是否全量。按美东已收盘交易日判定，落后一天也补。"""
    from app.utils import is_us_trading_day, last_completed_us_session

    if not daily and not monthly:
        return True, True
    if not daily:
        return True, True
    last = _daily_last_date(daily)
    if last is None:
        return True, True
    expected = last_completed_us_session()
    if last < expected:
        return True, False
    # 交易日盘中：已有昨收但尚无今日点 → 后台尽量写入盘中暂估
    today = _today_et()
    now = datetime.now(ET)
    if (
        is_us_trading_day(today)
        and last < today
        and (now.hour > 10 or (now.hour == 10 and now.minute >= 0))
        and now.hour < 20
    ):
        return True, False
    return False, False


def _maybe_kick_background_update(*, prefer_full: bool = False) -> None:
    """无数据或落后美东已收盘日时后台补数；同步 API 线程也能启动。"""
    global _BG_STARTED
    monthly = _stored_monthly_points()
    daily = _stored_daily_points()
    need, force_full = _needs_lev_update(daily, monthly)
    if prefer_full:
        force_full = True
    if not need and not prefer_full:
        return
    with _BG_LOCK:
        if _BG_STARTED:
            return
        _BG_STARTED = True

    async def _run_async() -> None:
        global _BG_STARTED
        try:
            await run_lev_etf_update(force_full=force_full)
        except Exception:
            logger.exception("lev-etf background update failed")
        finally:
            with _BG_LOCK:
                _BG_STARTED = False

    def _run_thread() -> None:
        global _BG_STARTED
        try:
            asyncio.run(run_lev_etf_update(force_full=force_full))
        except Exception:
            logger.exception("lev-etf background thread update failed")
        finally:
            with _BG_LOCK:
                _BG_STARTED = False

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_async())
        logger.info(
            "lev-etf background task queued (async) force_full=%s", force_full
        )
    except RuntimeError:
        threading.Thread(
            target=_run_thread, name="lev-etf-bg", daemon=True
        ).start()
        logger.info(
            "lev-etf background task started (thread) force_full=%s", force_full
        )


def _annotate_stale(payload: dict[str, Any], stored: list[dict]) -> dict[str, Any]:
    """旧数据照常返回，同时标记 stale 并触发后台补数。"""
    from app.utils import last_completed_us_session

    need, _ff = _needs_lev_update(stored, _stored_monthly_points())
    if not need:
        payload["stale"] = False
        payload["loading"] = False
        return payload
    _maybe_kick_background_update(prefer_full=False)
    expected = last_completed_us_session()
    last = _daily_last_date(stored)
    payload["stale"] = True
    payload["loading"] = True
    payload["expected_as_of"] = expected.isoformat()
    payload["note"] = (
        f"数据截至 {last.isoformat() if last else '—'}，"
        f"正在自动补到 {expected.isoformat()}（美东已收盘日）…"
    )
    return payload


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
        pts = cached["_all_points"]
        if granularity == "day" and not _soxl_coverage_ok(pts):
            _kick_soxl_hydrate_bg(pts, _stored_monthly_points())
        out = _payload_from_points(
            pts,
            granularity=granularity,
            year_filter=year_key,
            as_of_date=cached.get("as_of_date"),
            updated_at=cached.get("updated_at"),
            note=cached.get("note"),
        )
        return _annotate_stale(out, pts)

    if not stored:
        _maybe_kick_background_update(prefer_full=(granularity == "day"))
        return _empty_loading_payload(year_key, granularity)

    daily_src = stored if granularity == "day" else _stored_daily_points()
    monthly_src = stored if granularity == "month" else _stored_monthly_points()
    _kick_soxl_hydrate_bg(daily_src, monthly_src)

    payload = _payload_from_points(stored, granularity=granularity, year_filter="all")
    mem_payload = dict(payload)
    mem_payload["_all_points"] = stored
    mem["payload"] = mem_payload
    mem["ts"] = now
    out = _payload_from_points(
        stored,
        granularity=granularity,
        year_filter=year_key,
        as_of_date=payload.get("as_of_date"),
        updated_at=payload.get("updated_at"),
    )
    return _annotate_stale(out, stored)


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
    """回填或增量更新日度 + 月度序列。

    增量时以已有序列为底、新抓取覆盖同日，避免行情失败时把近窗数据冲掉。
    美东未收盘时若源返回「今日」K 线，会写入并标 is_partial。
    """
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

        # 盘中：今日未收盘则标 partial（有今日 bar 才标）
        now_et = datetime.now(ET)
        partial_date = None
        if today in daily_map and (
            now_et.hour < 16 or (now_et.hour == 16 and now_et.minute < 15)
        ):
            partial_date = today

        fresh_daily = to_daily_points(
            daily_map, start_date=start, partial_date=partial_date
        )

        if not fresh_daily and not existing_daily:
            return {
                "success": False,
                "error": "no bars fetched",
                "symbols_ok": len(bars),
                "symbols_total": len(tickers),
                "elapsed_sec": round(time.perf_counter() - t0, 1),
            }

        if existing_daily and not need_full:
            # 保留 lookback 窗内旧点，再用新抓取覆盖（失败时不丢近几日）
            daily_points = _merge_by_key(existing_daily, fresh_daily, "date")
            if start:
                daily_points = [
                    p
                    for p in daily_points
                    if str(p.get("date") or "") >= start.isoformat()
                ]
        else:
            daily_points = fresh_daily or existing_daily

        if not daily_points:
            return {
                "success": False,
                "error": "no bars fetched",
                "symbols_ok": len(bars),
                "symbols_total": len(tickers),
                "elapsed_sec": round(time.perf_counter() - t0, 1),
            }

        if fresh_daily:
            # 新抓取成功时：非 partial_date 的旧 is_partial 清掉
            for p in daily_points:
                if partial_date and p.get("date") == partial_date.isoformat():
                    p["is_partial"] = True
                elif p.get("date") != (partial_date.isoformat() if partial_date else None):
                    p["is_partial"] = False

        soxl_closes = soxl_closes_from_points(daily_points)
        soxl_closes.update(soxl_closes_from_bars(bars))
        try:
            first = date.fromisoformat(str(daily_points[0]["date"])[:10])
            last = date.fromisoformat(str(daily_points[-1]["date"])[:10])
            extra = await _fetch_soxl_closes(first - timedelta(days=10), last)
            soxl_closes.update(extra)
        except Exception as exc:
            logger.warning("lev-etf dedicated SOXL fetch failed: %s", exc)
        daily_points = attach_soxl_daily_returns(daily_points, soxl_closes)

        full_daily_map = daily_points_to_map(daily_points)
        start_month = str(basket.get("start_month") or "2023-01")
        monthly_points = aggregate_monthly(
            full_daily_map,
            start_month=start_month,
            as_of=max(full_daily_map) if full_daily_map else today,
            current_et_month=month_key(today),
        )
        monthly_points = attach_soxl_monthly_returns(monthly_points, soxl_closes)

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
            "partial_date": partial_date.isoformat() if partial_date else None,
            "fetched_days": len(fresh_daily),
            "db_upserted_daily": db_daily_n,
            "db_upserted": db_monthly_n,
            "elapsed_sec": round(time.perf_counter() - t0, 1),
            "latest": (daily_payload.get("stats") or {}).get("latest"),
        }
        logger.info("lev-etf update done: %s", result)
        return result
