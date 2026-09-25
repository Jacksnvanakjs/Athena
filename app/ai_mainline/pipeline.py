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

_CACHE: dict[str, Any] = {"ts": 0.0, "quote_ts": 0.0, "data": None}
_COMPUTE_BUDGET_SEC = 110.0  # 报价 + 日K 软截止；超时回退库内快照（不编造）
_PERIOD_BUDGET_SEC = 75.0
_PERIOD_BUDGET_SETTLE_SEC = 180.0  # 收盘结算窗给足时间写全日 K
_QUOTE_OVERLAY_SEC = 180.0  # 非盘中默认叠加间隔
_QUOTE_OVERLAY_RTH_SEC = 45.0  # 盘中更勤快刷 1D，避免卡在收盘快照


def _overlay_interval_sec(phase: str | None = None) -> float:
    phase = phase or _market_phase()
    if phase == "rth":
        return _QUOTE_OVERLAY_RTH_SEC
    if phase in {"pre_open", "settle", "overnight"}:
        return 90.0
    return _QUOTE_OVERLAY_SEC
_COV_OK = 6  # 5D×2+20D 覆盖评分门槛


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


def _sync_primary_from_themes(payload: dict[str, Any]) -> None:
    """把主题行上的当日 1D/广度同步回 primary，避免卡片仍是快照旧值、表格已是 0/6。"""
    themes = {
        t.get("key"): t for t in (payload.get("themes") or []) if t.get("key")
    }
    primary = payload.get("primary")
    if isinstance(primary, dict) and primary.get("key") in themes:
        t = themes[primary["key"]]
        for k in ("ret_1d", "breadth", "n_up", "n_valid"):
            if t.get(k) is not None:
                primary[k] = t[k]
        payload["primary"] = primary
    secondary = payload.get("secondary")
    if isinstance(secondary, dict) and secondary.get("key") in themes:
        t = themes[secondary["key"]]
        for k in ("ret_1d", "breadth", "n_up", "n_valid"):
            if t.get(k) is not None:
                secondary[k] = t[k]
        payload["secondary"] = secondary


def _compute_pulse_1d(themes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """当日 1D 最强子线（有效成分≥3），与 5 日主线可不同。"""
    best: dict[str, Any] | None = None
    for t in themes:
        if t.get("ret_1d") is None:
            continue
        if int(t.get("n_valid") or 0) < 3:
            continue
        if best is None or float(t["ret_1d"]) > float(best["ret_1d"]):
            best = t
    if not best:
        return None
    return {
        "key": best.get("key"),
        "name": best.get("name") or best.get("key"),
        "ret_1d": best.get("ret_1d"),
        "breadth": best.get("breadth"),
        "n_up": best.get("n_up"),
        "n_valid": best.get("n_valid"),
    }


def _is_1d_weak(primary: dict[str, Any] | None) -> bool:
    if not primary:
        return False
    n_valid = int(primary.get("n_valid") or 0)
    if n_valid < 3:
        return False
    n_up = primary.get("n_up")
    if n_up is not None and int(n_up) == 0:
        return True
    breadth = primary.get("breadth")
    if breadth is not None and float(breadth) < 0.35:
        return True
    return False


def _display_summary(payload: dict[str, Any]) -> str:
    primary = payload.get("primary")
    secondary = payload.get("secondary")
    status = payload.get("status") or "no_mainline"
    if not primary or status in ("no_mainline", "disabled", "error"):
        return (
            payload.get("summary")
            or "暂无明确主线（宏观/共振下跌或普涨）。相对强弱判断，非互斥；不构成投资建议。"
        )
    st_label = "已确认" if primary.get("status") == "confirmed" else "观察中"
    rel = primary.get("rel_5d")
    ret = primary.get("ret_5d")
    rel_s = f"{rel:+.1f}%" if rel is not None else "—"
    ret_s = f"{ret:+.1f}%" if ret is not None else "—"
    n_up, n_valid = primary.get("n_up"), primary.get("n_valid")
    if n_up is not None and n_valid:
        b1 = f"今日1D广度：上涨 {n_up}/{n_valid}"
    elif primary.get("breadth") is not None:
        b1 = f"今日1D广度：上涨占比 {float(primary['breadth']):.0%}"
    else:
        b1 = "今日1D广度：—"
    if _is_1d_weak(primary) and primary.get("status") in ("confirmed", "emerging"):
        b1 += "（偏弱，不改5日主线）"
    pulse = payload.get("pulse_1d")
    pulse_line = ""
    if pulse and pulse.get("key"):
        pr = pulse.get("ret_1d")
        pr_s = f"{pr:+.1f}%" if pr is not None else "—"
        if pulse.get("key") != primary.get("key"):
            pulse_line = f"今日1D脉搏：{pulse.get('name')}（{pr_s}）｜与5日主线不同\n"
        else:
            pulse_line = f"今日1D脉搏：与主线一致（{pr_s}）\n"
    sec_name = secondary["name"] if secondary else "无"
    return (
        f"5日主线：{primary['name']}（{st_label}）\n"
        f"近5日相对 AI 基准：{rel_s}｜板块 {ret_s}\n"
        f"{b1}\n"
        f"{pulse_line}"
        f"次强：{sec_name}\n"
        f"说明：主线按5日相对强弱确认；表格广度/1D是当日脉搏。相对强弱非互斥；不构成投资建议。"
    )


def _rotation_runs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐日 primary 压成连续区间，便于近2周轮动展示。"""
    runs: list[dict[str, Any]] = []
    for item in items:
        key = item.get("primary_key")
        name = item.get("primary_name") or key or "暂无明确主线"
        if not key:
            key = None
            name = "暂无明确主线"
        td = item.get("trade_date") or ""
        if runs and runs[-1].get("key") == key:
            runs[-1]["end"] = td
            runs[-1]["status"] = item.get("status")
            runs[-1]["days"] = int(runs[-1].get("days") or 0) + 1
            if item.get("streak_days") is not None:
                runs[-1]["streak_days"] = item.get("streak_days")
        else:
            runs.append(
                {
                    "key": key,
                    "name": name,
                    "start": td,
                    "end": td,
                    "status": item.get("status"),
                    "days": 1,
                    "streak_days": item.get("streak_days"),
                }
            )
    return runs


def _finalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """API 出口：同步 1D、脉搏、轮动；主线判定本身仍是 5D。"""
    if not payload:
        return payload
    if payload.get("enabled") is False or payload.get("status") == "disabled":
        return payload
    out = payload
    _sync_primary_from_themes(out)
    out["pulse_1d"] = _compute_pulse_1d(out.get("themes") or [])
    out["pulse_1d_weak"] = _is_1d_weak(out.get("primary"))
    out["mainline_basis"] = "rel_5d"
    out["summary"] = _display_summary(out)
    try:
        out["rotation_14d"] = _rotation_runs(history_primary(14))
    except Exception:
        logger.exception("ai_mainline rotation_14d failed")
        out.setdefault("rotation_14d", [])
    return out


def _market_phase(now: datetime | None = None) -> str:
    """美东时段：pre_open / rth / settle / overnight / closed。

    settle=收盘后约 2 小时，日 K 与主线快照必须赶上，不可长期吃旧缓存。
    周日 20:00 ET 起常见股票隔夜盘（通往周一），标为 overnight 而非 closed。
    """
    from app.utils import is_us_trading_day

    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    mins = now.hour * 60 + now.minute
    # 周日晚隔夜：不少券商 20:00 ET 起可交易，通往周一盘前
    if now.weekday() == 6 and mins >= 20 * 60:
        return "overnight"
    if not is_us_trading_day(now.date()):
        return "closed"
    if mins < 9 * 60 + 30:
        return "pre_open"
    if mins < 16 * 60:
        return "rth"
    if mins < 18 * 60:
        return "settle"
    return "overnight"


def _payload_trade_date(payload: dict[str, Any] | None) -> date | None:
    if not payload:
        return None
    raw = payload.get("trade_date")
    if not raw:
        return None
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _period_coverage(payload: dict[str, Any] | None) -> int:
    """有 5D 的主题数×2 + 有 20D 的主题数，用于判断快照是否残缺。"""
    if not payload:
        return 0
    n5 = n20 = 0
    for t in payload.get("themes") or []:
        if t.get("ret_5d") is not None:
            n5 += 1
        if t.get("ret_20d") is not None:
            n20 += 1
    return n5 * 2 + n20


def _needs_full_refresh(base: dict[str, Any] | None) -> bool:
    """主线以日线相对强弱为主；仅在缺数或落后于「已收盘交易日」时全量重算。"""
    from app.utils import last_completed_us_session

    if not base or _period_coverage(base) < _COV_OK:
        return True
    snap_d = _payload_trade_date(base)
    if snap_d is None:
        return True
    completed = last_completed_us_session()
    if snap_d < completed:
        return True
    phase = _market_phase()
    # 结算窗内若仍是「昨收快照」，必须拉今日日 K / 重算
    if phase == "settle" and snap_d < _today_et():
        return True
    return False


def _cache_ttl_sec(phase: str, payload: dict[str, Any] | None) -> float:
    """按时段决定缓存寿命：结算未追上今日时短 TTL，避免错过更新。"""
    cov = _period_coverage(payload)
    snap_d = _payload_trade_date(payload)
    today = _today_et()
    if phase == "settle":
        if cov < _COV_OK or not snap_d or snap_d < today:
            return 90.0
        return 300.0
    if phase == "rth":
        return 900.0  # 盘中 5D/20D 几乎不变，15 分钟内不重打日 K
    if phase == "pre_open":
        return 1200.0
    return 1800.0  # overnight / closed


def _pick_best_base(
    cached: dict[str, Any] | None, db_payload: dict[str, Any] | None
) -> dict[str, Any] | None:
    c_cov = _period_coverage(cached)
    d_cov = _period_coverage(db_payload)
    if c_cov >= _COV_OK and c_cov >= d_cov:
        return cached
    if d_cov >= _COV_OK:
        return db_payload
    if c_cov > 0 and c_cov >= d_cov:
        return cached
    return db_payload if d_cov > 0 else cached


def _live_1d_active(phase: str | None = None) -> bool:
    """1D 即时叠加：交易日全时段 + 周日晚隔夜；周六全日休市不拉。"""
    phase = phase or _market_phase()
    return phase in {"pre_open", "rth", "settle", "overnight"}


def _session_tip(phase: str) -> str:
    if phase == "pre_open":
        return "盘前已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。"
    if phase == "settle":
        return "盘后已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。"
    if phase == "overnight":
        return "隔夜/周末夜盘已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。"
    if phase == "rth":
        return "盘中已刷新 1D 报价；5D/20D 与主线判定沿用日线快照。"
    return "5D/20D 与主线判定沿用日线快照。"


async def _quotes_for_1d_overlay(phase: str) -> tuple[dict[str, Any], str]:
    """盘中用多源快路；扩展时段优先 Yahoo 会话价，避免东财停在常规收盘涨跌。"""
    from app.heatmap import get_quotes_for_symbols, get_quotes_session_aware

    symbols = all_symbols()
    if phase == "rth":
        return await get_quotes_for_symbols(symbols, allow_slow_fill=False)
    return await get_quotes_session_aware(symbols)


async def _overlay_live_1d(
    payload: dict[str, Any], *, phase: str | None = None
) -> tuple[dict[str, Any], bool]:
    """叠加即时报价 1D；不重拉日 K、不改主线排名（仍按 5D）。

    返回 (payload, ok)。ok=False 时调用方不要推进 quote_ts，以便尽快重试。
    """
    import asyncio

    phase = phase or _market_phase()
    try:
        quotes, src = await asyncio.wait_for(
            _quotes_for_1d_overlay(phase),
            timeout=28.0 if phase != "rth" else 18.0,
        )
    except Exception as exc:
        logger.info("mainline 1d overlay skipped: %s", exc)
        out = dict(payload)
        out["live_1d"] = False
        msg = f"1D 即时行情暂未刷新（{type(exc).__name__}）"
        out["live_1d_note"] = msg
        base_note = (out.get("note") or "").strip()
        if msg not in base_note:
            out["note"] = f"{base_note} {msg}".strip() if base_note else msg
        return out, False
    if not quotes:
        out = dict(payload)
        out["live_1d"] = False
        msg = "1D 即时行情暂无返回，仍展示上一版"
        out["live_1d_note"] = msg
        base_note = (out.get("note") or "").strip()
        if msg not in base_note:
            out["note"] = f"{base_note} {msg}".strip() if base_note else msg
        return out, False

    out = dict(payload)
    themes_out: list[dict[str, Any]] = []
    for theme in payload.get("themes") or []:
        row = dict(theme)
        members = []
        ret_1d_list: list[float] = []
        up = 0
        for m in theme.get("members") or []:
            mem = dict(m)
            sym = (mem.get("symbol") or "").upper()
            q = quotes.get(sym)
            if q and q.get("change_pct") is not None:
                chg = round(float(q["change_pct"]), 2)
                mem["ret_1d"] = chg
                if q.get("quote_time"):
                    mem["quote_time"] = q["quote_time"]
                if q.get("quote_time_et"):
                    mem["quote_time_et"] = q["quote_time_et"]
                ret_1d_list.append(chg)
                if chg > 0:
                    up += 1
            members.append(mem)
        row["members"] = members
        if ret_1d_list:
            row["ret_1d"] = round(sum(ret_1d_list) / len(ret_1d_list), 2)
            row["n_up"] = up
            row["n_valid"] = len(ret_1d_list)
            row["breadth"] = round(up / len(ret_1d_list), 4)
        themes_out.append(row)
    out["themes"] = themes_out

    bench = dict(payload.get("bench") or {})
    b1: list[float] = []
    for q in quotes.values():
        if q and q.get("change_pct") is not None:
            b1.append(float(q["change_pct"]))
    if b1:
        bench["ret_1d"] = round(sum(b1) / len(b1), 2)
    out["bench"] = bench
    # as_of / updated_bj 仍是请求/计算时刻；1D 数据时刻单独字段
    out["as_of"] = _as_of_iso()
    out["updated_bj"] = now_beijing().strftime("%Y-%m-%d %H:%M")
    qtimes = _quote_data_times(quotes)
    # 扩展时段：丢掉「美东16:00/北京04:00」收盘印记，避免伪装成实时
    if phase != "rth":
        from app.heatmap import _quote_looks_like_rth_close

        session_quotes = {
            k: v for k, v in quotes.items() if v and not _quote_looks_like_rth_close(v)
        }
        if session_quotes:
            qtimes = _quote_data_times(session_quotes)
        else:
            qtimes = {"data_time_1d_bj": None, "data_time_1d_et": None}
    out.update(qtimes)
    out.update(_daily_session_close_times(out.get("trade_date")))
    # 有行情但源没给 quote_time 时，绝不能回落到「美东收盘→北京次日04:00」
    if not out.get("data_time_1d_bj") and not out.get("data_time_1d_et"):
        out["data_time_1d_bj"] = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        out["data_time_1d_et"] = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")
        out["data_time_1d_source"] = "overlay_refresh"
    else:
        out["data_time_1d_source"] = "live_quote"
    out["live_1d"] = True
    if "rth_stamp_heavy" in str(src) and phase != "rth":
        out["live_1d_note"] = "扩展时段会话源偏弱，部分报价仍可能停在常规收盘"
    else:
        out.pop("live_1d_note", None)
    out["quote_count"] = len(quotes)
    out["quote_total"] = len(all_symbols())
    out["session_phase"] = phase
    prev_src = payload.get("source") or "snapshot"
    # 去掉旧的 live_1d 后缀再挂新源，避免叠多层
    base_src = str(prev_src).split("+live_1d")[0]
    out["source"] = f"{base_src}+live_1d({src})"
    out["stale"] = False
    note = (payload.get("note") or "").strip()
    for old in (
        "盘中已刷新 1D 报价；5D/20D 与主线判定沿用日线快照。",
        "盘前已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "盘后已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "隔夜展示最近盘后/收盘 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "隔夜/周末夜盘已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "1D 即时行情暂无返回，仍展示上一版",
    ):
        note = note.replace(old, "").strip()
    # 去掉失败提示整句（可能带异常名）
    if "1D 即时行情暂未刷新" in note:
        chunks = [c.strip() for c in note.replace("。", "。|").split("|") if c.strip()]
        note = " ".join(c for c in chunks if "1D 即时行情暂未刷新" not in c).strip()
    tip = _session_tip(phase)
    out["note"] = f"{note} {tip}".strip() if note else tip
    out["success"] = True
    return out, True


def _theme_name_map() -> dict[str, str]:
    return {t["key"]: t.get("name") or t["key"] for t in enabled_themes()}


def _quote_data_times(quotes: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    """从行情行取「数据本身」时间（最新一条），不是服务端计算时刻。"""
    bj_times = [str(q["quote_time"]) for q in quotes.values() if q and q.get("quote_time")]
    et_times = [
        str(q["quote_time_et"]) for q in quotes.values() if q and q.get("quote_time_et")
    ]
    return {
        "data_time_1d_bj": max(bj_times) if bj_times else None,
        "data_time_1d_et": max(et_times) if et_times else None,
    }


def _daily_session_close_times(trade_date: date | str | None) -> dict[str, str | None]:
    """日线 5D/20D 对应的美东收盘时刻（16:00 ET）及其北京时间。"""
    if trade_date is None:
        return {"data_time_daily_bj": None, "data_time_daily_et": None}
    if isinstance(trade_date, str):
        try:
            trade_date = date.fromisoformat(trade_date[:10])
        except ValueError:
            return {"data_time_daily_bj": None, "data_time_daily_et": None}
    dt_et = datetime(
        trade_date.year, trade_date.month, trade_date.day, 16, 0, tzinfo=ET
    )
    bj = ZoneInfo("Asia/Shanghai")
    return {
        "data_time_daily_bj": dt_et.astimezone(bj).strftime("%Y-%m-%d %H:%M"),
        "data_time_daily_et": dt_et.strftime("%Y-%m-%d %H:%M %Z"),
    }


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
    payload = {
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
    payload.update(_daily_session_close_times(trade_s))
    # 无即时报价时：1D 也按该交易日收盘理解（勿被前端当成实时）
    payload["data_time_1d_bj"] = payload.get("data_time_daily_bj")
    payload["data_time_1d_et"] = payload.get("data_time_daily_et")
    payload["data_time_1d_source"] = "session_close"
    payload["live_1d"] = False
    return payload


async def _compute_mainline_fresh(
    *,
    period_budget: float | None = _PERIOD_BUDGET_SEC,
) -> dict[str, Any]:
    """拉行情并计算；缺数保持空，不编造。"""
    from app.database import SessionLocal
    from app.heatmap import fetch_period_returns
    from app.market_data import fetch_quotes

    themes_cfg = enabled_themes()
    symbols = all_symbols()
    quotes, source = await fetch_quotes(symbols, allow_slow_fill=False)
    try:
        period = await fetch_period_returns(symbols, budget_sec=period_budget)
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
        **_quote_data_times(quotes),
        **_daily_session_close_times(today),
    }


async def compute_mainline(force: bool = False) -> dict[str, Any]:
    """页面优先完整日线快照；收盘结算窗全量更新；盘中仅叠加 1D。"""
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
    phase = _market_phase()
    cached = _CACHE.get("data")
    db_payload = _payload_from_db_snapshots()
    base = _pick_best_base(cached, db_payload)

    if not force:
        ttl = _cache_ttl_sec(phase, cached if cached else base)
        cache_age = now - float(_CACHE.get("ts") or 0)
        needs_full = _needs_full_refresh(base)

        if not needs_full and cached and cache_age < ttl:
            quote_age = now - float(_CACHE.get("quote_ts") or 0)
            need_overlay = _live_1d_active(phase) and (
                quote_age >= _overlay_interval_sec(phase) or not cached.get("live_1d")
            )
            if need_overlay:
                overlaid, ok = await _overlay_live_1d(cached, phase=phase)
                _CACHE["data"] = overlaid
                if ok:
                    _CACHE["quote_ts"] = now
                return _finalize_payload(overlaid)
            return _finalize_payload(cached)

        if not needs_full and base is not None:
            out = base
            if _live_1d_active(phase):
                out, ok = await _overlay_live_1d(base, phase=phase)
                if ok:
                    _CACHE["quote_ts"] = now
            elif out is db_payload and out is not None:
                out = dict(out)
                out["note"] = (out.get("note") or "") or (
                    "展示库内主线日线快照（主线按日变化，未强制重拉外网）。"
                )
            _CACHE["ts"] = now
            _CACHE["data"] = out
            return _finalize_payload(out)
        logger.info(
            "ai_mainline full refresh phase=%s snap=%s cov=%s",
            phase,
            _payload_trade_date(base),
            _period_coverage(base),
        )

    period_budget = (
        _PERIOD_BUDGET_SETTLE_SEC
        if force or phase == "settle"
        else _PERIOD_BUDGET_SEC
    )
    compute_budget = max(_COMPUTE_BUDGET_SEC, period_budget + 40.0)

    try:
        payload = await asyncio.wait_for(
            _compute_mainline_fresh(period_budget=period_budget),
            timeout=compute_budget,
        )
    except asyncio.TimeoutError:
        logger.warning("ai_mainline compute timeout after %.0fs", compute_budget)
        if base and _period_coverage(base) >= _COV_OK:
            kept = _stale_payload(
                base,
                f"行情拉取超时（>{int(compute_budget)}s），展示上一版可靠结果，未使用估算数据。",
            )
            _CACHE["ts"] = now if phase != "settle" else now - 60
            _CACHE["data"] = kept
            return _finalize_payload(kept)
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
        if base and _period_coverage(base) >= _COV_OK:
            return _finalize_payload(
                _stale_payload(
                    base,
                    f"本次刷新失败（{type(exc).__name__}），展示上一版可靠结果。",
                )
            )
        raise

    new_cov = _period_coverage(payload)
    old_cov = _period_coverage(base)
    if base and old_cov >= _COV_OK and new_cov < max(4, old_cov // 2) and not force:
        logger.warning(
            "ai_mainline period coverage regress %s→%s; keep prior",
            old_cov,
            new_cov,
        )
        kept = _stale_payload(
            base,
            f"本次 5D/20D 覆盖不足（评分 {new_cov}/{old_cov}），保留完整日线快照；结算窗将自动重试。",
        )
        _CACHE["ts"] = now if phase != "settle" else now - 60
        _CACHE["data"] = kept
        return _finalize_payload(kept)

    if new_cov < _COV_OK and base and old_cov > new_cov and not force:
        kept = _stale_payload(
            base,
            f"实时日K未补齐（覆盖 {new_cov}），展示库内/缓存完整快照。",
        )
        _CACHE["ts"] = now if phase != "settle" else now - 60
        _CACHE["data"] = kept
        return _finalize_payload(kept)

    _CACHE["ts"] = now
    if _live_1d_active(phase):
        payload, ok = await _overlay_live_1d(payload, phase=phase)
        if ok:
            _CACHE["quote_ts"] = now
    else:
        _CACHE["quote_ts"] = now
    _CACHE["data"] = payload
    return _finalize_payload(payload)



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
    """收盘后写日快照 + 可选推送。先补全日 K，再强制重算，避免错过收盘更新。"""
    if not AI_MAINLINE_ENABLED:
        return {"success": True, "skipped": True, "reason": "disabled"}

    from app.database import SessionLocal
    from app.heatmap import refresh_period_daily_closes
    from app.utils import is_us_trading_day

    today = _today_et()
    if not force and not is_us_trading_day(today):
        return {
            "success": True,
            "skipped": True,
            "reason": "非美股交易日",
            "trade_date": today.isoformat(),
        }

    period_info: dict[str, Any] = {}
    try:
        period_info = await refresh_period_daily_closes()
        logger.info("ai_mainline daily: period closes %s", period_info)
    except Exception as exc:
        logger.warning("ai_mainline daily: period refresh failed: %s", exc)
        period_info = {"success": False, "error": str(exc)}

    payload = await compute_mainline(force=True)
    if not payload.get("success") and _period_coverage(payload) < _COV_OK:
        return {
            "success": False,
            "error": "compute_failed",
            "payload": payload,
            "period": period_info,
        }

    # 残缺结果不落库，避免脏快照盖住昨日完整数据
    if _period_coverage(payload) < _COV_OK:
        return {
            "success": False,
            "error": "coverage_insufficient",
            "coverage": _period_coverage(payload),
            "period": period_info,
            "payload_status": payload.get("status"),
        }

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
        "period": period_info,
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
