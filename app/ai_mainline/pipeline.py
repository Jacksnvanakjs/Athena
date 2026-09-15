"""AI 主线：拉行情 → 算指标 → 排名 →（可选）落库 / 推送。"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.ai_mainline.baskets import all_symbols, enabled_themes, theme_by_key
from app.ai_mainline.config import (
    AI_MAINLINE_CONFIRM_DAYS,
    AI_MAINLINE_ENABLED,
    AI_MAINLINE_PUSH_COOLDOWN_DAYS,
    AI_MAINLINE_PUSH_ENABLED,
    META_KEY,
)
from app.ai_mainline.metrics import attach_relative, compute_benchmark, theme_metrics
from app.ai_mainline.ranking import judge_mainline, rank_themes
from app.utils import now_beijing

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL = 300  # 5 分钟：重复打开页不重打全市场
_COMPUTE_BUDGET_SEC = 110.0  # 报价 + 日K 软截止；超时回退库内快照（不编造）
_PERIOD_BUDGET_SEC = 75.0


def _today_et() -> date:
    return datetime.now(ET).date()


def _as_of_iso() -> str:
    return datetime.now(ET).isoformat(timespec="seconds")


def _stale_payload(base: dict[str, Any], note: str) -> dict[str, Any]:
    out = dict(base)
    out["stale"] = True
    out["note"] = note
    out["success"] = True
    return out


def _theme_name_map() -> dict[str, str]:
    return {t["key"]: t.get("name") or t["key"] for t in enabled_themes()}


def _basket_members(theme_key: str) -> list[dict[str, Any]]:
    """从篮子配置还原成分（快照未存 members 时的兜底）。"""
    theme = theme_by_key(theme_key)
    if not theme:
        return []
    out: list[dict[str, Any]] = []
    for t in theme.get("tickers") or []:
        sym = (t.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out.append(
            {
                "symbol": sym,
                "name": (t.get("name") or "").strip(),
                "ret_1d": None,
            }
        )
    return out


def _payload_from_db_snapshots() -> dict[str, Any] | None:
    """用库内已落库的主线日快照组装页面（真实历史，非估算）。

    历史行只存了涨跌指标、未存 members；组装时从篮子补齐成分，
    避免页面「成分」列空白（超时回退快照时尤其明显）。
    """
    from app.database import AiMainlineDailySnapshot, SessionLocal

    try:
        with SessionLocal() as db:
            latest = (
                db.query(AiMainlineDailySnapshot.trade_date)
                .order_by(AiMainlineDailySnapshot.trade_date.desc())
                .limit(1)
                .scalar()
            )
            if not latest:
                return None
            rows = (
                db.query(AiMainlineDailySnapshot)
                .filter(AiMainlineDailySnapshot.trade_date == latest)
                .all()
            )
    except Exception as exc:
        logger.warning("load mainline db snapshots failed: %s", exc)
        return None

    if not rows:
        return None

    names = _theme_name_map()
    meta_row = next((r for r in rows if r.theme_key == META_KEY), None)
    meta: dict[str, Any] = {}
    if meta_row and meta_row.payload_json:
        try:
            meta = json.loads(meta_row.payload_json)
        except json.JSONDecodeError:
            meta = {}

    themes_out: list[dict[str, Any]] = []
    primary_key = meta.get("primary_key")
    secondary_key = meta.get("secondary_key")
    status = meta.get("status") or "no_mainline"

    for r in rows:
        if r.theme_key == META_KEY:
            continue
        key = r.theme_key
        role = None
        status_label = "—"
        if primary_key and key == primary_key:
            role = "primary"
            status_label = (
                "主线·已确认" if status == "confirmed" else "主线·观察中"
            )
        elif secondary_key and key == secondary_key:
            role = "secondary"
            status_label = "次强"

        members: list[dict[str, Any]] = []
        leaders: list[str] = []
        if r.payload_json:
            try:
                extra = json.loads(r.payload_json)
                if isinstance(extra.get("members"), list) and extra["members"]:
                    members = extra["members"]
                leaders = list(extra.get("leaders") or [])
                if extra.get("name"):
                    names[key] = extra["name"]
            except json.JSONDecodeError:
                pass
        if not members:
            members = _basket_members(key)

        themes_out.append(
            {
                "key": key,
                "name": names.get(key, key),
                "ret_1d": r.ret_1d,
                "ret_5d": r.ret_5d,
                "ret_20d": r.ret_20d,
                "rel_1d": r.rel_1d,
                "rel_5d": r.rel_5d,
                "rel_20d": r.rel_20d,
                "breadth": r.breadth,
                "rank_5d": r.rank_5d,
                "n_valid": r.n_valid,
                "streak_days": meta.get("streak_days") if key == primary_key else 0,
                "role": role,
                "status_label": status_label,
                "members": members,
                "tickers": [m.get("symbol") for m in members if m.get("symbol")],
                "leaders": leaders,
            }
        )

    themes_out.sort(
        key=lambda x: (x.get("rank_5d") is None, x.get("rank_5d") or 999)
    )
    if not themes_out:
        return None

    primary = next((t for t in themes_out if t.get("role") == "primary"), None)
    secondary = next((t for t in themes_out if t.get("role") == "secondary"), None)
    if primary:
        primary = {
            **primary,
            "status": status if status in ("confirmed", "emerging") else "emerging",
        }

    trade_s = latest.isoformat() if hasattr(latest, "isoformat") else str(latest)
    return {
        "success": True,
        "enabled": True,
        "as_of": _as_of_iso(),
        "trade_date": trade_s,
        "source": "db_snapshot",
        "quote_count": None,
        "quote_total": None,
        "bench": {
            "ret_1d": meta_row.ret_1d if meta_row else None,
            "ret_5d": meta.get("bench_ret_5d")
            or (meta_row.ret_5d if meta_row else None),
            "ret_20d": meta_row.ret_20d if meta_row else None,
        },
        "primary": primary,
        "secondary": secondary,
        "status": status,
        "streak_days": meta.get("streak_days"),
        "summary": meta.get("summary")
        or "展示最近一次已落库主线快照（真实收盘数据）。",
        "themes": themes_out,
        "disclaimer": "相对强弱判断，非互斥；不构成投资建议。",
        "updated_bj": now_beijing().strftime("%Y-%m-%d %H:%M"),
        "stale": True,
        "note": f"实时行情较慢，展示库内 {trade_s} 主线快照（非估算）。",
    }


async def _compute_mainline_fresh() -> dict[str, Any]:
    """拉行情并计算；缺数保持空，不编造。"""
    from app.database import SessionLocal
    from app.heatmap import fetch_period_returns
    from app.market_data import fetch_quotes

    themes_cfg = enabled_themes()
    symbols = all_symbols()
    quotes, source = await fetch_quotes(symbols, allow_slow_fill=False)
    # 勿用外层 wait_for 整段掐死：period 自带软截止，超时后仍返回已算到的部分（缺数保持 None，不编造）
    try:
        period = await fetch_period_returns(symbols, budget_sec=_PERIOD_BUDGET_SEC)
    except Exception as exc:
        logger.warning("period returns failed: %s", exc)
        period = {s: {"ret_5d": None, "ret_20d": None} for s in symbols}

    raw_themes = [theme_metrics(t, quotes, period) for t in themes_cfg]
    bench = compute_benchmark(themes_cfg, quotes, period)
    with_rel = attach_relative(raw_themes, bench)
    ranked = rank_themes(with_rel)

    today = _today_et()
    prior_streak: dict[str, int] = {}
    try:
        with SessionLocal() as db:
            prior_streak = _load_streak_before_today(db, today)
    except Exception as exc:
        logger.warning("load streak failed: %s", exc)

    streak = _streak_including_today(ranked, prior_streak)
    judged = judge_mainline(ranked, streak)

    primary_key = (judged.get("primary") or {}).get("key")
    secondary_key = (judged.get("secondary") or {}).get("key")
    themes_out: list[dict[str, Any]] = []
    for t in ranked:
        row = dict(t)
        row["streak_days"] = streak.get(t["key"], 0)
        if primary_key and t["key"] == primary_key:
            row["role"] = "primary"
            row["status_label"] = (
                "主线·已确认"
                if (judged.get("primary") or {}).get("status") == "confirmed"
                else "主线·观察中"
            )
        elif secondary_key and t["key"] == secondary_key:
            row["role"] = "secondary"
            row["status_label"] = "次强"
        else:
            row["role"] = None
            row["status_label"] = "—"
        themes_out.append(row)

    themes_out.sort(
        key=lambda x: (x.get("rank_5d") is None, x.get("rank_5d") or 999)
    )

    return {
        "success": True,
        "enabled": True,
        "as_of": _as_of_iso(),
        "trade_date": today.isoformat(),
        "source": f"heatmap+period({source})",
        "quote_count": len(quotes),
        "quote_total": len(symbols),
        "bench": bench,
        "primary": judged.get("primary"),
        "secondary": judged.get("secondary"),
        "status": judged.get("status"),
        "streak_days": judged.get("streak_days"),
        "summary": judged.get("summary"),
        "themes": themes_out,
        "disclaimer": "相对强弱判断，非互斥；不构成投资建议。",
        "updated_bj": now_beijing().strftime("%Y-%m-%d %H:%M"),
        "stale": False,
    }


async def compute_mainline(force: bool = False) -> dict[str, Any]:
    """盘中/API：计算当前主线排名（带短缓存）。超时回退内存/库内真实快照。"""
    import asyncio

    if not AI_MAINLINE_ENABLED:
        return {
            "success": False,
            "enabled": False,
            "as_of": _as_of_iso(),
            "note": "AI 主线监控已关闭",
            "themes": [],
            "primary": None,
            "secondary": None,
            "status": "disabled",
            "summary": "AI 主线监控未启用。",
        }

    now = time.time()
    cached = _CACHE.get("data")
    if (
        not force
        and cached
        and now - float(_CACHE.get("ts") or 0) < _CACHE_TTL
    ):
        return cached

    try:
        payload = await asyncio.wait_for(
            _compute_mainline_fresh(), timeout=_COMPUTE_BUDGET_SEC
        )
    except asyncio.TimeoutError:
        logger.warning("ai_mainline compute timeout after %.0fs", _COMPUTE_BUDGET_SEC)
        if cached:
            return _stale_payload(
                cached,
                f"行情拉取超时（>{int(_COMPUTE_BUDGET_SEC)}s），展示上一版可靠结果，未使用估算数据。",
            )
        db_payload = _payload_from_db_snapshots()
        if db_payload:
            _CACHE["ts"] = now
            _CACHE["data"] = db_payload
            return db_payload
        return {
            "success": False,
            "enabled": True,
            "as_of": _as_of_iso(),
            "themes": [],
            "primary": None,
            "secondary": None,
            "status": "error",
            "summary": "主线行情拉取超时且无可用快照，请稍后刷新。未编造任何涨跌数据。",
            "note": "timeout",
        }
    except Exception as exc:
        logger.exception("ai_mainline compute failed: %s", exc)
        if cached:
            return _stale_payload(
                cached,
                f"本次刷新失败（{type(exc).__name__}），展示上一版可靠结果。",
            )
        db_payload = _payload_from_db_snapshots()
        if db_payload:
            return db_payload
        raise

    _CACHE["ts"] = now
    _CACHE["data"] = payload
    return payload


def _load_streak_before_today(db, today: date) -> dict[str, int]:
    """读最近 CONFIRM_DAYS+5 日快照，估算各子线连续 Top2+rel>0 天数（不含今日）。"""
    from app.database import AiMainlineDailySnapshot

    since = today - timedelta(days=AI_MAINLINE_CONFIRM_DAYS + 10)
    rows = (
        db.query(AiMainlineDailySnapshot)
        .filter(
            AiMainlineDailySnapshot.trade_date >= since,
            AiMainlineDailySnapshot.trade_date < today,
            AiMainlineDailySnapshot.theme_key != META_KEY,
        )
        .order_by(AiMainlineDailySnapshot.trade_date.desc())
        .all()
    )
    # date -> {key: row}
    by_date: dict[date, dict[str, Any]] = {}
    for r in rows:
        by_date.setdefault(r.trade_date, {})[r.theme_key] = r

    dates = sorted(by_date.keys(), reverse=True)
    streak: dict[str, int] = {}
    if not dates:
        return streak

    # 从最近一日往回，对每个 key 数连续满足天数
    keys = set()
    for dmap in by_date.values():
        keys.update(dmap.keys())

    for key in keys:
        n = 0
        for d in dates:
            row = by_date[d].get(key)
            if not row:
                break
            rank = row.rank_5d
            rel = row.rel_5d
            if rank is not None and rank <= 2 and rel is not None and float(rel) > 0:
                n += 1
            else:
                break
        streak[key] = n
    return streak


def _streak_including_today(
    themes: list[dict[str, Any]],
    prior: dict[str, int],
) -> dict[str, int]:
    out = dict(prior)
    for t in themes:
        key = t.get("key")
        if not key:
            continue
        rank = t.get("rank_5d")
        rel = t.get("rel_5d")
        ok = rank is not None and rank <= 2 and rel is not None and float(rel) > 0
        if ok:
            out[key] = int(prior.get(key) or 0) + 1
        else:
            out[key] = 0
    return out


def _upsert_daily(db, trade_date: date, payload: dict[str, Any]) -> int:
    from app.database import AiMainlineDailySnapshot

    db.query(AiMainlineDailySnapshot).filter(
        AiMainlineDailySnapshot.trade_date == trade_date
    ).delete()
    n = 0
    for t in payload.get("themes") or []:
        db.add(
            AiMainlineDailySnapshot(
                trade_date=trade_date,
                theme_key=t["key"],
                ret_1d=t.get("ret_1d"),
                ret_5d=t.get("ret_5d"),
                ret_20d=t.get("ret_20d"),
                rel_1d=t.get("rel_1d"),
                rel_5d=t.get("rel_5d"),
                rel_20d=t.get("rel_20d"),
                breadth=t.get("breadth"),
                rank_5d=t.get("rank_5d"),
                n_valid=t.get("n_valid") or 0,
                payload_json=json.dumps(
                    {
                        "leaders": t.get("leaders"),
                        "name": t.get("name"),
                        "members": t.get("members") or [],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        n += 1

    meta = {
        "primary_key": (payload.get("primary") or {}).get("key"),
        "primary_name": (payload.get("primary") or {}).get("name"),
        "status": payload.get("status"),
        "secondary_key": (payload.get("secondary") or {}).get("key"),
        "secondary_name": (payload.get("secondary") or {}).get("name"),
        "bench_ret_5d": (payload.get("bench") or {}).get("ret_5d"),
        "streak_days": payload.get("streak_days"),
        "summary": payload.get("summary"),
    }
    db.add(
        AiMainlineDailySnapshot(
            trade_date=trade_date,
            theme_key=META_KEY,
            ret_1d=(payload.get("bench") or {}).get("ret_1d"),
            ret_5d=(payload.get("bench") or {}).get("ret_5d"),
            ret_20d=(payload.get("bench") or {}).get("ret_20d"),
            rel_1d=None,
            rel_5d=None,
            rel_20d=None,
            breadth=None,
            rank_5d=None,
            n_valid=(payload.get("bench") or {}).get("n_valid") or 0,
            payload_json=json.dumps(meta, ensure_ascii=False),
        )
    )
    n += 1
    db.commit()
    return n


async def _maybe_push(db, trade_date: date, payload: dict[str, Any]) -> dict[str, Any]:
    if not AI_MAINLINE_PUSH_ENABLED:
        return {"pushed": False, "reason": "disabled"}

    primary = payload.get("primary") or {}
    if primary.get("status") != "confirmed" or not primary.get("key"):
        return {"pushed": False, "reason": "not_confirmed"}

    from app.database import AiMainlineDailySnapshot
    from app.notifier import notify

    # 上次 confirmed meta
    prev_metas = (
        db.query(AiMainlineDailySnapshot)
        .filter(
            AiMainlineDailySnapshot.theme_key == META_KEY,
            AiMainlineDailySnapshot.trade_date < trade_date,
        )
        .order_by(AiMainlineDailySnapshot.trade_date.desc())
        .limit(AI_MAINLINE_PUSH_COOLDOWN_DAYS + 5)
        .all()
    )
    last_confirmed_key = None
    last_confirmed_name = None
    last_confirmed_date = None
    for m in prev_metas:
        try:
            data = json.loads(m.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if data.get("status") == "confirmed" and data.get("primary_key"):
            last_confirmed_key = data["primary_key"]
            last_confirmed_name = data.get("primary_name")
            last_confirmed_date = m.trade_date
            break

    new_key = primary["key"]
    if last_confirmed_key == new_key:
        return {"pushed": False, "reason": "same_mainline"}

    if last_confirmed_date and (trade_date - last_confirmed_date).days < AI_MAINLINE_PUSH_COOLDOWN_DAYS:
        # 冷却期内若从未有过 confirmed，仍可推；有过则跳过
        if last_confirmed_key:
            return {"pushed": False, "reason": "cooldown"}

    from app.ai_mainline.push import build_mainline_switch_push

    title, body = build_mainline_switch_push(
        last_confirmed_name,
        primary,
        payload.get("themes") or [],
        payload.get("summary") or "",
    )
    results = await notify(title, body)
    ok = any(results.values()) if results else False
    return {"pushed": bool(ok), "title": title, "channels": results}


async def run_ai_mainline_daily(force: bool = False) -> dict[str, Any]:
    """收盘后写日快照 + 可选推送。"""
    if not AI_MAINLINE_ENABLED:
        return {"success": True, "skipped": True, "reason": "disabled"}

    from app.database import SessionLocal
    from app.utils import is_us_trading_day

    today = _today_et()
    if not force and not is_us_trading_day(today):
        return {
            "success": True,
            "skipped": True,
            "reason": "非美股交易日",
            "trade_date": today.isoformat(),
        }

    payload = await compute_mainline(force=True)
    if not payload.get("success"):
        return {"success": False, "error": "compute_failed", "payload": payload}

    with SessionLocal() as db:
        saved = _upsert_daily(db, today, payload)
        push_info = await _maybe_push(db, today, payload)

    return {
        "success": True,
        "trade_date": today.isoformat(),
        "saved_rows": saved,
        "status": payload.get("status"),
        "primary": (payload.get("primary") or {}).get("key"),
        "push": push_info,
    }


def history_primary(days: int = 30) -> list[dict[str, Any]]:
    from app.database import AiMainlineDailySnapshot, SessionLocal

    since = _today_et() - timedelta(days=days)
    with SessionLocal() as db:
        rows = (
            db.query(AiMainlineDailySnapshot)
            .filter(
                AiMainlineDailySnapshot.theme_key == META_KEY,
                AiMainlineDailySnapshot.trade_date >= since,
            )
            .order_by(AiMainlineDailySnapshot.trade_date.asc())
            .all()
        )
    out = []
    for r in rows:
        try:
            meta = json.loads(r.payload_json or "{}")
        except json.JSONDecodeError:
            meta = {}
        out.append(
            {
                "trade_date": r.trade_date.isoformat(),
                "primary_key": meta.get("primary_key"),
                "primary_name": meta.get("primary_name"),
                "status": meta.get("status"),
                "secondary_key": meta.get("secondary_key"),
                "bench_ret_5d": meta.get("bench_ret_5d"),
                "streak_days": meta.get("streak_days"),
            }
        )
    return out
