"""AI 主线：拉行情 → 算指标 → 排名 →（可选）落库 / 推送。"""

from __future__ import annotations

import json
import logging
import re
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

_CACHE: dict[str, Any] = {"ts": 0.0, "quote_ts": 0.0, "data": None, "phase": None}
_OVERLAY_BG = False
_OVERLAY_BG_STARTED = 0.0
_FULL_BG = False
# 后台叠加卡住超过此时长允许重新调度（避免 _OVERLAY_BG 永久挡住盘后刷新）
_OVERLAY_BG_STALE_SEC = 75.0
# 轮动/库快照内存缓存：Turso 慢时禁止每次 /api/ai-mainline 同步打库卡整表
_ROT_CACHE: dict[str, Any] = {
    "primary": [],
    "pulse": [],
    "primary_ts": 0.0,
    "pulse_ts": 0.0,
}
_DB_SNAP_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_ROT_CACHE_TTL_SEC = 600.0
_DB_SNAP_CACHE_TTL_SEC = 120.0
_DB_QUERY_BUDGET_SEC = 4.0
_COMPUTE_BUDGET_SEC = 110.0  # 报价 + 日K 软截止；超时回退库内快照（不编造）
_PERIOD_BUDGET_SEC = 75.0
_PERIOD_BUDGET_SETTLE_SEC = 180.0  # 收盘结算窗给足时间写全日 K
_QUOTE_OVERLAY_SEC = 180.0  # 非盘中默认叠加间隔
_QUOTE_OVERLAY_RTH_SEC = 45.0  # 盘中更勤快刷 1D，避免卡在收盘快照
# 美东时段切换点（分钟）：盘前开 / 开盘 / 收盘进盘后 / 进夜盘
_PHASE_SWITCH_MINUTES = (4 * 60, 9 * 60 + 30, 16 * 60, 20 * 60)
_PHASE_SWITCH_WINDOW_MIN = 10  # 切换点前后 N 分钟加紧检测
_SHORT_ROT_MIN_RET_1D = 2.0
_SHORT_ROT_MIN_BREADTH = 1.0


def _near_phase_switch(now: datetime | None = None, *, window_min: int = _PHASE_SWITCH_WINDOW_MIN) -> bool:
    """是否接近盘前/开盘/收盘/夜盘切换点（重点盯这些窗口）。"""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    mins = now.hour * 60 + now.minute
    for b in _PHASE_SWITCH_MINUTES:
        delta = abs(mins - b)
        # 跨日：23:55 接近次日 04:00 不算；00:05 接近 20:00 用环形距离
        wrap = min(delta, 24 * 60 - delta)
        if wrap <= window_min:
            return True
    return False


def _overlay_interval_sec(phase: str | None = None) -> float:
    phase = phase or _market_phase()
    if _near_phase_switch():
        return 30.0  # 切换窗口加紧
    if phase == "rth":
        return _QUOTE_OVERLAY_RTH_SEC
    if phase == "pre_open":
        return 45.0
    if phase == "settle":
        return 60.0
    if phase == "overnight":
        return 60.0
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


def _short_rotation_eligible(row: dict[str, Any] | None) -> bool:
    """短线轮动：有效成分≥3、广度=1（全上涨）、1D≥2%。"""
    if not row or row.get("ret_1d") is None:
        return False
    n_valid = int(row.get("n_valid") or 0)
    if n_valid < 3:
        return False
    if float(row["ret_1d"]) < _SHORT_ROT_MIN_RET_1D:
        return False
    n_up = row.get("n_up")
    if n_up is not None:
        return int(n_up) == n_valid
    breadth = row.get("breadth")
    return breadth is not None and float(breadth) >= _SHORT_ROT_MIN_BREADTH - 1e-9


def _empty_short_rotation_day(trade_date: str) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "primary_key": None,
        "primary_name": "暂无明确短线",
        "status": "pulse",
    }


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
            name = item.get("primary_name") or "暂无明确主线"
        td = item.get("trade_date") or ""
        if runs and runs[-1].get("key") == key:
            runs[-1]["end"] = td
            runs[-1]["status"] = item.get("status")
            runs[-1]["days"] = int(runs[-1].get("days") or 0) + 1
            if item.get("streak_days") is not None:
                runs[-1]["streak_days"] = item.get("streak_days")
            if item.get("ret_1d") is not None:
                runs[-1]["ret_1d"] = item.get("ret_1d")
            if item.get("breadth") is not None:
                runs[-1]["breadth"] = item.get("breadth")
            if item.get("n_valid") is not None:
                runs[-1]["n_valid"] = item.get("n_valid")
            if item.get("n_up") is not None:
                runs[-1]["n_up"] = item.get("n_up")
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
                    "ret_1d": item.get("ret_1d"),
                    "breadth": item.get("breadth"),
                    "n_valid": item.get("n_valid"),
                    "n_up": item.get("n_up"),
                }
            )
    return runs


def _finalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """API 出口：同步 1D、脉搏、轮动；主线判定本身仍是 5D。"""
    if not payload:
        return payload
    if payload.get("enabled") is False or payload.get("status") == "disabled":
        return payload
    out = dict(payload)
    # 出口时刻以当前时段为准，避免缓存仍挂 overnight 而页面已是盘前
    phase = _market_phase()
    out["session_phase"] = phase
    # 日线收盘时刻兜底：页面「数据更新时间」绝不能空白
    if not out.get("data_time_daily_bj") and out.get("trade_date"):
        out.update(_daily_session_close_times(out.get("trade_date")))
    if not out.get("updated_bj"):
        out["updated_bj"] = now_beijing().strftime("%Y-%m-%d %H:%M")
    # 安全网：不得把滞后/收盘印记标成「最新1D」；非盘中 pending 时保留数字占位
    if _live_1d_active(phase):
        bad_live = bool(out.get("live_1d")) and (
            _1d_lag_too_large(out, phase) or not _has_ext_live_1d(out, phase)
        )
        if bad_live:
            keep = _has_holdable_1d(out)
            out = _mark_1d_pending(
                _strip_stale_1d_fields(out, phase=phase),
                note="1D 印记非扩展最新，正在拉取盘后/会话价…",
                keep_last_1d=keep,
            )
        elif out.get("1d_pending"):
            keep = _has_holdable_1d(out) or bool(out.get("1d_hold"))
            out["1d_fresh"] = False
            out["live_1d"] = False
            if keep:
                out["1d_hold"] = True
            else:
                out = _clear_intraday_1d_metrics(out)
        elif out.get("live_1d"):
            out["1d_fresh"] = True
            out["1d_pending"] = False
            out.pop("1d_hold", None)
        else:
            # 尚未 live：标 pending；非盘中保留快照 1D 占位，盘中才清空防昨收冒充
            out["1d_fresh"] = False
            out["1d_pending"] = True
            if _has_holdable_1d(out):
                out["1d_hold"] = True
            else:
                out = _clear_intraday_1d_metrics(out)
            if not out.get("live_1d_note"):
                out["live_1d_note"] = "1D 正在拉取最新报价…"
    else:
        out["1d_fresh"] = True
        out["1d_pending"] = False
    _sync_primary_from_themes(out)
    out["pulse_1d"] = _compute_pulse_1d(out.get("themes") or [])
    out["pulse_1d_weak"] = _is_1d_weak(out.get("primary"))
    out["mainline_basis"] = "rel_5d"
    out["summary"] = _display_summary(out)
    # 轮动历史走内存缓存，禁止每次出口同步打 Turso（慢库曾把整表拖到数分钟）
    try:
        out["rotation_14d"] = _rotation_runs(_cached_history_primary(14))
    except Exception:
        logger.exception("ai_mainline rotation_14d failed")
        out.setdefault("rotation_14d", [])
    try:
        out["rotation_1d"] = _rotation_runs(
            _pulse_history_with_today(out, days=10, use_cache=True)
        )
    except Exception:
        logger.exception("ai_mainline rotation_1d failed")
        out.setdefault("rotation_1d", [])
    return _ensure_display_times(out)


def _market_phase(now: datetime | None = None) -> str:
    """美东时段：pre_open / rth / settle / overnight / closed。

    - settle=盘后 16:00–20:00（日 K 须赶上）
    - overnight=夜盘 ATS 20:00–次日 04:00（含周日晚通往周一）
    - pre_open=盘前 04:00–09:30
    """
    from datetime import timedelta

    from app.utils import is_us_trading_day

    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)
    mins = now.hour * 60 + now.minute
    wd = now.weekday()

    # 夜盘 ATS：20:00–04:00（跨午夜）
    if mins >= 20 * 60:
        if wd == 6 or is_us_trading_day(now.date()):
            return "overnight"
        return "closed"
    if mins < 4 * 60:
        yday = now.date() - timedelta(days=1)
        # 交易日凌晨 / 周五夜盘延至周六凌晨 / 周日夜盘延至周一凌晨
        if is_us_trading_day(now.date()) or is_us_trading_day(yday) or yday.weekday() == 6:
            return "overnight"
        return "closed"

    if not is_us_trading_day(now.date()):
        return "closed"
    if mins < 9 * 60 + 30:
        return "pre_open"
    if mins < 16 * 60:
        return "rth"
    return "settle"


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


def _basket_keys_mismatch(base: dict[str, Any] | None) -> bool:
    """篮子增删主题后，旧快照缺行 → 强制全量重算。"""
    if not base:
        return True
    have = {
        str(t.get("key"))
        for t in (base.get("themes") or [])
        if t.get("key")
    }
    want = {str(t.get("key")) for t in enabled_themes() if t.get("key")}
    return have != want


def _needs_full_refresh(base: dict[str, Any] | None) -> bool:
    """主线以日线相对强弱为主；仅在缺数或落后于「已收盘交易日」时全量重算。"""
    from app.utils import last_completed_us_session

    if not base or _period_coverage(base) < _COV_OK:
        return True
    if _basket_keys_mismatch(base):
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
    """是否应叠加 1D 即时报价。

    口径（与日历无关）：
    - 盘前 / 盘中 / 盘后：免费源有数就必须拉并展示（含周末休市展示上周五盘后）。
    - 夜盘 ATS：无免费源则不硬拉付费 ATS，回退「最近盘后」；禁止停在常规收盘 04:00。
    """
    _ = phase  # 各时段均尝试；具体用哪档价由过滤/源决定
    return True


def _is_ext_phase(phase: str) -> bool:
    """非盘中：需要扩展印记（盘前/盘后），拒绝把常规收盘04:00当最新。"""
    return phase in {"pre_open", "settle", "overnight", "closed"}


def _session_tip(phase: str) -> str:
    if phase == "pre_open":
        return "已刷新盘前1D（相对昨收）；5D/20D 与主线判定沿用日线快照。"
    if phase == "settle":
        return "已刷新盘后1D（相对昨收）；5D/20D 与主线判定沿用日线快照。"
    if phase in ("overnight", "closed"):
        return "夜盘ATS无免费源，已用最近盘后1D；5D/20D 与主线判定沿用日线快照。"
    if phase == "rth":
        return "已刷新盘中1D；5D/20D 与主线判定沿用日线快照。"
    return "5D/20D 与主线判定沿用日线快照。"


def _parse_member_quote_dt(row: dict[str, Any] | None) -> datetime | None:
    """解析行情行上的美东/北京时间戳。"""
    if not row:
        return None
    et = str(row.get("quote_time_et") or "").strip()
    bj = str(row.get("quote_time") or "").strip()
    for raw, tz in ((et, ET), (bj, ZoneInfo("Asia/Shanghai"))):
        if not raw:
            continue
        # "2026-09-24 19:59:00 EDT" / "2026-09-25 07:59:00"
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?", raw)
        if m:
            try:
                sec = int(m.group(6) or 0)
                return datetime(
                    int(m.group(1)),
                    int(m.group(2)),
                    int(m.group(3)),
                    int(m.group(4)),
                    int(m.group(5)),
                    sec,
                    tzinfo=tz,
                ).astimezone(ET)
            except ValueError:
                pass
        # 新浪等："Sep 25 04:39AM EDT"
        m2 = re.search(
            r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
            r"(\d{1,2})\s+(\d{1,2}):(\d{2})\s*(AM|PM)",
            raw,
            re.I,
        )
        if m2:
            months = {
                "jan": 1,
                "feb": 2,
                "mar": 3,
                "apr": 4,
                "may": 5,
                "jun": 6,
                "jul": 7,
                "aug": 8,
                "sep": 9,
                "oct": 10,
                "nov": 11,
                "dec": 12,
            }
            try:
                mon = months[m2.group(1).lower()[:3]]
                hour = int(m2.group(3)) % 12
                if m2.group(5).upper() == "PM":
                    hour += 12
                year = datetime.now(ET).year
                # 若带年份则用
                ym = re.search(r"\b(20\d{2})\b", raw)
                if ym:
                    year = int(ym.group(1))
                return datetime(
                    year,
                    mon,
                    int(m2.group(2)),
                    hour,
                    int(m2.group(4)),
                    0,
                    tzinfo=tz,
                ).astimezone(ET)
            except (ValueError, KeyError):
                continue
    return None


def _quote_stamp_too_old(row: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    """超过约 2.5 个自然日的印记视为过期（避免一直挂着 22 号盘后）。"""
    dt = _parse_member_quote_dt(row)
    if dt is None:
        return False
    now = now or datetime.now(ET)
    return (now - dt).total_seconds() > 60 * 60 * 60  # 60h


# 盘中 1D：日线收盘源不可用；东财仅在无成交印记时不可信
# （Finnhub 常限流，有今日 9:30+ 印记的东财涨跌应可用）
_RTH_DAILY_BAR_SOURCES = frozenset({"tushare", "akshare"})


def _rth_quote_trusted(row: dict[str, Any] | None) -> bool:
    """盘中可信报价：会话印记已在外层过滤；此处拦日线源与无戳东财/备用源。"""
    if not row or row.get("change_pct") is None:
        return False
    src = str(row.get("quote_source") or "").strip().lower()
    if src in _RTH_DAILY_BAR_SOURCES:
        return False
    if src in ("eastmoney", "alt"):
        # 东财/备用源无真实成交印记不可信（alt 曾用墙钟伪造时间）
        return _parse_member_quote_dt(row) is not None
    return True


def _theme_1d_fingerprint(payload: dict[str, Any] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in (payload or {}).get("themes") or []:
        key = t.get("key")
        if not key or t.get("ret_1d") is None:
            continue
        try:
            out[str(key)] = round(float(t["ret_1d"]), 2)
        except (TypeError, ValueError):
            continue
    return out


def _is_prior_session_1d_replay(
    prior: dict[str, float] | None, themes: list[dict[str, Any]]
) -> bool:
    """盘中叠加结果若几乎等于昨收快照 1D，视为源把昨涨跌当成今日。"""
    if not prior or len(prior) < 3:
        return False
    compared = 0
    matched = 0
    for t in themes:
        key = str(t.get("key") or "")
        if key not in prior or t.get("ret_1d") is None:
            continue
        compared += 1
        try:
            if abs(float(t["ret_1d"]) - float(prior[key])) <= 0.05:
                matched += 1
        except (TypeError, ValueError):
            continue
    return compared >= 3 and matched / compared >= 0.8


def _filter_quotes_for_1d(
    quotes: dict[str, dict[str, Any]], phase: str
) -> tuple[dict[str, dict[str, Any]], str]:
    """夜盘/盘前/盘后：只用非收盘印记；盘前/盘后绝不把 RTH 收盘当 live。

    夜盘无扩展价时才允许 rth_fallback（夜盘前最后一档）。
    盘中：必须是今日 9:30 后印记；日线源/无戳东财剔除。
    """
    from app.heatmap import _quote_looks_like_rth_close

    now = datetime.now(ET)
    # live 只认可解析的真实成交印记；无戳行（TradingView/Finviz 等）不得进入 fresh，
    # 否则会占坑并把仍有效的会话印记挤掉，或用抓取墙钟冒充「最新1D」。
    stamped = {
        k: v
        for k, v in quotes.items()
        if v
        and v.get("change_pct") is not None
        and _parse_member_quote_dt(v) is not None
        and not _quote_stamp_too_old(v, now=now)
    }
    if not stamped:
        # 周末夜盘：60h 边界上的真实印记仍可回退；依然拒绝无戳
        stamped = {
            k: v
            for k, v in quotes.items()
            if v
            and v.get("change_pct") is not None
            and _parse_member_quote_dt(v) is not None
        }
    if phase == "rth":
        live = {
            k: v
            for k, v in stamped.items()
            if v and _quote_dt_in_current_session(v, "rth")
        }
        trusted = {k: v for k, v in live.items() if _rth_quote_trusted(v)}
        if trusted:
            return trusted, "rth"
        if live:
            # 有今日印记但全是日线源 → 仍待可靠源
            return {}, "em_stale"
        return {}, "no_rth"
    if not _is_ext_phase(phase):
        return stamped, "rth"
    non_rth = {
        k: v
        for k, v in stamped.items()
        if v and not _quote_looks_like_rth_close(v)
    }
    if non_rth:
        # 夜盘/盘后/休市：缺扩展价的票用最近收盘补洞，避免 CNBC 漏票整只灰掉
        if phase in ("overnight", "settle", "closed"):
            out = dict(non_rth)
            for k, v in stamped.items():
                if k not in out:
                    out[k] = v
            # 休市/夜盘：盘后中段印记规范为 20:00（北京08:00），避免长期停在 05:33
            if phase in ("overnight", "closed"):
                out = {k: _normalize_completed_post_quote(v) for k, v in out.items()}
            kind = "session+rth_holes" if len(out) > len(non_rth) else "session"
            return out, kind
        return non_rth, "session"
    # 盘前/夜盘/休市：无扩展印记就失败重试，禁止把北京 04:00 常规收盘标成「最新1D」
    if phase in ("pre_open", "overnight", "closed"):
        return {}, "no_ext"
    # 盘后刚开始扩展源未齐时，允许短暂回退收盘价（须有真实印记）；仍标 rth_fallback 供上层慎用
    if phase == "settle":
        return stamped, "rth_fallback" if stamped else "no_ext"
    return stamped, "rth_fallback" if stamped else "no_ext"


def _quote_dt_in_current_session(
    row: dict[str, Any] | None, phase: str, *, now: datetime | None = None
) -> bool:
    """报价印记是否属于当前时段（盘中必须是今日 9:30 之后）。"""
    dt = _parse_member_quote_dt(row)
    if dt is None:
        return False
    now = now or datetime.now(ET)
    today = now.date()
    if phase == "rth":
        rth_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        return dt.date() == today and dt >= rth_open
    if phase == "pre_open":
        start = now.replace(hour=4, minute=0, second=0, microsecond=0)
        return dt.date() == today and dt >= start
    if phase == "settle":
        start = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return dt.date() == today and dt >= start
    return True


def _strip_stale_1d_fields(
    payload: dict[str, Any], *, phase: str | None = None
) -> dict[str, Any]:
    """叠加失败时清掉过期/收盘假 live 的 1D 戳，避免盘前挂「25凌晨4点」。"""
    from app.heatmap import _quote_looks_like_rth_close

    out = dict(payload)
    now = datetime.now(ET)
    stamp = {
        "quote_time": out.get("data_time_1d_bj"),
        "quote_time_et": out.get("data_time_1d_et"),
    }
    clear = _quote_stamp_too_old(stamp, now=now)
    # 扩展时段/休市：常规收盘印记一律清掉（勿继续展示为当前 live 1D）
    if phase and _is_ext_phase(phase) and _quote_looks_like_rth_close(stamp):
        clear = True
    # 印记相对当前时段已滞后：禁止继续展示为「最新1D」
    if phase and phase in ("pre_open", "rth", "settle") and _1d_lag_too_large(out, phase):
        clear = True
    if clear:
        out["data_time_1d_bj"] = None
        out["data_time_1d_et"] = None
        out["data_time_1d_source"] = None
        out["live_1d"] = False
        out["1d_fresh"] = False
        out["1d_pending"] = True
        # 清 1D 戳后补日线时刻，避免页面只剩「—」
        if not out.get("data_time_daily_bj") and out.get("trade_date"):
            out.update(_daily_session_close_times(out.get("trade_date")))
        if not out.get("updated_bj"):
            out["updated_bj"] = now_beijing().strftime("%Y-%m-%d %H:%M")
    themes_out: list[dict[str, Any]] = []
    for theme in out.get("themes") or []:
        row = dict(theme)
        members = []
        for m in theme.get("members") or []:
            mem = dict(m)
            drop = _quote_stamp_too_old(mem, now=now)
            if phase and _is_ext_phase(phase) and _quote_looks_like_rth_close(mem):
                drop = True
            if drop:
                mem.pop("quote_time", None)
                mem.pop("quote_time_et", None)
            members.append(mem)
        row["members"] = members
        themes_out.append(row)
    out["themes"] = themes_out
    return out


def _is_incomplete_post_stamp(
    row: dict[str, Any] | None, *, now: datetime | None = None
) -> bool:
    """盘后会话已结束后仍停在 16:00–20:00 内的「最后一笔」(如北京05:33)。"""
    dt = _parse_member_quote_dt(row)
    if dt is None:
        return False
    mins = dt.hour * 60 + dt.minute
    if mins < 16 * 60 or mins >= 20 * 60:
        return False
    now = now or datetime.now(ET)
    post_end = dt.replace(hour=20, minute=0, second=0, microsecond=0)
    return now >= post_end


def _normalize_completed_post_quote(row: dict[str, Any]) -> dict[str, Any]:
    """盘后已结束后：印记规范为当日美东 20:00（北京次日 08:00），价格不变。"""
    if not _is_incomplete_post_stamp(row):
        return row
    dt = _parse_member_quote_dt(row)
    if dt is None:
        return row
    post_end = dt.replace(hour=20, minute=0, second=0, microsecond=0)
    out = dict(row)
    bj = ZoneInfo("Asia/Shanghai")
    out["quote_time_et"] = post_end.strftime("%Y-%m-%d %H:%M:%S %Z")
    out["quote_time"] = post_end.astimezone(bj).strftime("%Y-%m-%d %H:%M:%S")
    return out


def _1d_stamp_mismatch_phase(payload: dict[str, Any] | None, phase: str) -> bool:
    """缓存 1D 印记与当前时段不符 → 必须重拉（避免盘前还挂昨收 04:00）。"""
    if not payload:
        return True
    if payload.get("session_phase") and payload.get("session_phase") != phase:
        return True
    row = {
        "quote_time": payload.get("data_time_1d_bj"),
        "quote_time_et": payload.get("data_time_1d_et"),
    }
    from app.heatmap import _quote_looks_like_rth_close

    if phase == "pre_open":
        # 盘前：昨收 16:00/北京次日04:00 一律视为过期
        if _quote_looks_like_rth_close(row):
            return True
        dt = _parse_member_quote_dt(row)
        if dt is None:
            return not payload.get("live_1d")
        # 早于今日美东 04:00 的印记（昨盘后等）在盘前也要换新
        pre_start = datetime.now(ET).replace(hour=4, minute=0, second=0, microsecond=0)
        return dt < pre_start
    if phase == "rth":
        dt = _parse_member_quote_dt(row)
        if dt is None:
            return True
        rth_open = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
        return dt < rth_open
    if phase == "settle":
        # 盘后：不应还停在常规收盘 16:00
        return _quote_looks_like_rth_close(row)
    if phase in ("overnight", "closed"):
        # 夜盘/休市：收盘印记/快照一律视为不符（须换成盘后扩展戳，禁止停在北京04:00）
        if _quote_looks_like_rth_close(row):
            return True
        if (payload.get("data_time_1d_source") or "") == "session_close":
            return True
        # 停在盘后中段 17:33/北京05:33 → 重拉并规范到 20:00/北京08:00
        if _is_incomplete_post_stamp(row):
            return True
        return not bool(payload.get("live_1d"))
    return False


def _1d_lag_too_large(payload: dict[str, Any] | None, phase: str) -> bool:
    """相对「此刻」印记过旧则重拉。

    注意：盘后/夜盘成交稀疏，阈值不能按盘中秒级套。过严会反复 pending，
    页面回退展示北京 04:00 常规收盘戳，看起来像「1D 不更新」。
    """
    if not payload:
        return True
    dt = _parse_member_quote_dt(
        {
            "quote_time": payload.get("data_time_1d_bj"),
            "quote_time_et": payload.get("data_time_1d_et"),
        }
    )
    if dt is None:
        # 盘中/盘前/盘后无印记：不能当 live（东财昨涨跌常无戳）
        if phase in ("pre_open", "rth", "settle"):
            return True
        return not bool(payload.get("live_1d"))
    lag = (datetime.now(ET) - dt).total_seconds()
    if lag < 0:
        return False
    if phase == "pre_open":
        return lag > 15 * 60  # 盘前稀疏，15 分钟内的扩展价仍算有效
    if phase == "rth":
        return lag > 120
    if phase == "settle":
        # 盘后 16:00–20:00：成交不如盘中密，20 分钟内扩展价可展示
        return lag > 20 * 60
    # overnight/closed：公共源无 ATS，周五盘后戳要撑过周末；拦「跨过一个完整交易日」的陈旧缓存
    return lag > 60 * 60 * 36


def _has_holdable_1d(payload: dict[str, Any] | None) -> bool:
    """刷新等待时可继续展示的 1D 数字（不必先打成「—」）。

    - 已 live / 1d_hold：保留。
    - 非盘中（盘后/夜盘/休市/盘前）：库快照上的 ret_1d 也可占位，
      避免 overlay 未完成时整列「—」（盘中仍禁止把昨收快照当今日）。
    """
    if not payload:
        return False
    has_num = any(
        (t or {}).get("ret_1d") is not None for t in (payload.get("themes") or [])
    )
    if not has_num:
        return False
    if payload.get("live_1d") or payload.get("1d_hold"):
        return True
    phase = _market_phase()
    return phase != "rth"


def _clear_intraday_1d_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    """去掉昨收/快照上的 1D，避免盘中把上一日涨跌当成今日脉搏。"""
    out = dict(payload)
    if not out.get("prior_session_1d") and not payload.get("live_1d"):
        prior = _theme_1d_fingerprint(payload)
        if prior:
            out["prior_session_1d"] = prior
    themes_out: list[dict[str, Any]] = []
    for theme in out.get("themes") or []:
        row = dict(theme)
        row["ret_1d"] = None
        row["rel_1d"] = None
        row["n_up"] = None
        row["breadth"] = None
        members = []
        for m in row.get("members") or []:
            mem = dict(m)
            mem["ret_1d"] = None
            mem.pop("quote_time", None)
            mem.pop("quote_time_et", None)
            members.append(mem)
        row["members"] = members
        themes_out.append(row)
    out["themes"] = themes_out
    if isinstance(out.get("bench"), dict):
        bench = dict(out["bench"])
        bench["ret_1d"] = None
        out["bench"] = bench
    out["pulse_1d"] = None
    return out


def _backfill_1d_times_from_members(payload: dict[str, Any]) -> dict[str, Any]:
    """顶层 1D 时刻被清空时，从成分印记回填（优先非收盘扩展戳）。"""
    from app.heatmap import _quote_looks_like_rth_close

    out = dict(payload)
    if out.get("data_time_1d_bj") or out.get("data_time_1d_et"):
        return out
    rows: dict[str, dict[str, Any]] = {}
    for theme in out.get("themes") or []:
        for m in theme.get("members") or []:
            sym = (m.get("symbol") or "").upper()
            if not sym:
                continue
            if m.get("quote_time") or m.get("quote_time_et"):
                rows[sym] = m
    if not rows:
        return out
    ext = {k: v for k, v in rows.items() if not _quote_looks_like_rth_close(v)}
    times = _quote_data_times(ext or rows)
    if times.get("data_time_1d_bj") or times.get("data_time_1d_et"):
        out.update(times)
        if not out.get("data_time_1d_source"):
            out["data_time_1d_source"] = "member_quote"
    return out


def _ensure_display_times(payload: dict[str, Any]) -> dict[str, Any]:
    """出口保证「数据时间」有可展示字段，避免页面只剩「—」。"""
    out = _backfill_1d_times_from_members(payload)
    if not out.get("data_time_daily_bj") and out.get("trade_date"):
        out.update(_daily_session_close_times(out.get("trade_date")))
    if not out.get("updated_bj"):
        out["updated_bj"] = now_beijing().strftime("%Y-%m-%d %H:%M")
    return out


def _mark_1d_pending(
    payload: dict[str, Any],
    *,
    note: str | None = None,
    keep_last_1d: bool | None = None,
) -> dict[str, Any]:
    """出口态：不宣称 live，强制前端快轮询。

    从未叠过今日 1D 时清掉快照涨跌，避免把昨收当今日；
    已有上一轮会话 1D 时保留数字作占位，只把状态标成拉取中。
    """
    out = dict(payload)
    if keep_last_1d is None:
        keep_last_1d = _has_holdable_1d(payload)
    if not out.get("prior_session_1d") and not payload.get("live_1d"):
        prior = _theme_1d_fingerprint(payload)
        if prior:
            out["prior_session_1d"] = prior
    out["live_1d"] = False
    out["1d_fresh"] = False
    out["1d_pending"] = True
    phase = _market_phase()
    if _live_1d_active(phase) and not keep_last_1d:
        out = _clear_intraday_1d_metrics(out)
        out.pop("1d_hold", None)
    elif keep_last_1d:
        out["1d_hold"] = True
    out = _ensure_display_times(out)
    msg = (note or out.get("live_1d_note") or "1D 正在拉取最新报价…").strip()
    out["live_1d_note"] = msg
    return out


def _needs_1d_refresh(
    payload: dict[str, Any] | None,
    phase: str,
    *,
    quote_age: float,
) -> tuple[bool, str]:
    """自动检测：要不要重拉 1D。返回 (需要, 原因)。

    着重：时段切换、印记与时段不符、印记滞后、常规轮询间隔。
    """
    if not _live_1d_active(phase):
        return False, ""
    if not payload:
        return True, "no_payload"
    if payload.get("1d_pending"):
        return True, "pending"
    if not payload.get("live_1d"):
        return True, "no_live"
    prev = payload.get("session_phase")
    if prev and prev != phase:
        return True, f"phase_switch:{prev}->{phase}"
    if _CACHE.get("phase") and _CACHE.get("phase") != phase:
        return True, f"cache_phase:{_CACHE.get('phase')}->{phase}"
    if _1d_stamp_mismatch_phase(payload, phase):
        return True, "stamp_mismatch"
    if _1d_lag_too_large(payload, phase):
        return True, "stamp_lag"
    interval = _overlay_interval_sec(phase)
    if quote_age >= interval:
        return True, f"interval:{int(quote_age)}s>={int(interval)}s"
    if _near_phase_switch() and quote_age >= 25.0:
        return True, "near_boundary"
    return False, ""


async def _quotes_for_1d_overlay(phase: str) -> tuple[dict[str, Any], str]:
    """按时段拉 1D 报价（有数必拉；与是否周末无关）。

    数据源：
    - 盘中 rth：东财/Finnhub 快路，缺口再补会话源。
    - 盘前/盘后/夜盘/休市：``get_quotes_session_aware``
      （CNBC 扩展价 → 新浪 → Yahoo → Finnhub…）。
      夜盘 ATS 无免费源时，CNBC/新浪返回的是**最近盘后**，不是付费 ATS。
    """
    from app.heatmap import get_quotes_for_symbols, get_quotes_session_aware

    symbols = all_symbols()
    # 非盘中（含周末 closed）：一律走会话源，确保盘前/盘后有数就拉到
    if phase != "rth":
        return await get_quotes_session_aware(symbols)

    import asyncio

    quotes, src = await get_quotes_for_symbols(symbols, allow_slow_fill=False)
    holes = [
        s
        for s in symbols
        if not (
            quotes.get(s)
            and quotes[s].get("change_pct") is not None
            and _rth_quote_trusted(quotes[s])
            and _quote_dt_in_current_session(quotes[s], "rth")
        )
    ]
    if not holes:
        return quotes, src
    try:
        from app.heatmap import _fill_quote_holes

        more, src2 = await asyncio.wait_for(
            _fill_quote_holes(holes, quotes, timeout=9.0), timeout=10.0
        )
    except Exception as exc:
        logger.info("rth 1d hole fill skipped: %s", exc)
        return quotes, src
    if not more:
        return quotes, src
    merged = dict(quotes)
    added = 0
    for sym, row in more.items():
        prev = merged.get(sym)
        if prev is None:
            merged[sym] = row
            added += 1
            continue
        prev_ok = _rth_quote_trusted(prev) and _quote_dt_in_current_session(
            prev, "rth"
        )
        new_ok = _rth_quote_trusted(row) and _quote_dt_in_current_session(row, "rth")
        if new_ok and not prev_ok:
            merged[sym] = row
            added += 1
    label = f"{src}+holes({src2}+a{added})" if added else src
    return merged, label


async def _overlay_live_1d(
    payload: dict[str, Any], *, phase: str | None = None
) -> tuple[dict[str, Any], bool]:
    """叠加即时报价 1D；不重拉日 K、不改主线排名（仍按 5D）。

    返回 (payload, ok)。ok=False 时调用方不要推进 quote_ts，以便尽快重试。
    夜盘无 ATS 时用夜盘前最新盘后价；失败则清掉过期时间戳，不继续挂旧印记。
    行情源若仍返回滞后印记，一律 ok=False，禁止把陈旧戳标成 live。
    """
    import asyncio

    phase = phase or _market_phase()
    try:
        quotes, src = await asyncio.wait_for(
            _quotes_for_1d_overlay(phase),
            timeout=22.0 if phase != "rth" else 32.0,
        )
    except Exception as exc:
        logger.info("mainline 1d overlay skipped: %s", exc)
        keep = _has_holdable_1d(payload)
        out = _strip_stale_1d_fields(dict(payload), phase=phase)
        out = _mark_1d_pending(
            out,
            note=f"1D 即时行情暂未刷新（{type(exc).__name__}）",
            keep_last_1d=keep,
        )
        base_note = (out.get("note") or "").strip()
        msg = out["live_1d_note"]
        if msg not in base_note:
            out["note"] = f"{base_note} {msg}".strip() if base_note else msg
        return out, False
    if not quotes:
        keep = _has_holdable_1d(payload)
        out = _strip_stale_1d_fields(dict(payload), phase=phase)
        out = _mark_1d_pending(
            out, note="1D 即时行情暂无返回，正在重试", keep_last_1d=keep
        )
        base_note = (out.get("note") or "").strip()
        msg = out["live_1d_note"]
        if msg not in base_note:
            out["note"] = f"{base_note} {msg}".strip() if base_note else msg
        return out, False

    use_quotes, qkind = _filter_quotes_for_1d(quotes, phase)
    if not use_quotes:
        out = _strip_stale_1d_fields(dict(payload), phase=phase)
        msg = (
            "盘前/盘后暂无扩展价，已拒绝常规收盘印记，待下一轮刷新"
            if qkind == "no_ext"
            else "盘中暂无今日常规交易报价，已拒绝昨收 1D，待下一轮刷新"
            if qkind == "no_rth"
            else "盘中已拒绝东财昨收涨跌，待会话源刷新 1D"
            if qkind == "em_stale"
            else "1D 行情印记过期，已丢弃，待下一轮刷新"
        )
        return _mark_1d_pending(out, note=msg, keep_last_1d=_has_holdable_1d(payload)), False

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
            q = use_quotes.get(sym)
            if q and q.get("change_pct") is not None:
                chg = round(float(q["change_pct"]), 2)
                mem["ret_1d"] = chg
                if q.get("quote_time"):
                    mem["quote_time"] = q["quote_time"]
                else:
                    mem.pop("quote_time", None)
                if q.get("quote_time_et"):
                    mem["quote_time_et"] = q["quote_time_et"]
                else:
                    mem.pop("quote_time_et", None)
                ret_1d_list.append(chg)
                if chg > 0:
                    up += 1
            else:
                # 本轮漏票：保留上一轮成员 1D，避免局部成功把灰票打穿
                if mem.get("ret_1d") is not None:
                    try:
                        chg = round(float(mem["ret_1d"]), 2)
                        ret_1d_list.append(chg)
                        if chg > 0:
                            up += 1
                    except (TypeError, ValueError):
                        mem["ret_1d"] = None
                else:
                    mem["ret_1d"] = None
                    mem.pop("quote_time", None)
                    mem.pop("quote_time_et", None)
            members.append(mem)
        row["members"] = members
        if ret_1d_list:
            row["ret_1d"] = round(sum(ret_1d_list) / len(ret_1d_list), 2)
            row["n_up"] = up
            row["n_valid"] = len(ret_1d_list)
            row["breadth"] = round(up / len(ret_1d_list), 4)
        else:
            row["ret_1d"] = None
            row["n_up"] = None
            row["n_valid"] = 0
            row["breadth"] = None
        themes_out.append(row)
    prior = payload.get("prior_session_1d") or _theme_1d_fingerprint(payload)
    if phase == "rth" and _is_prior_session_1d_replay(prior, themes_out):
        stripped = _strip_stale_1d_fields(dict(payload), phase=phase)
        if prior and not stripped.get("prior_session_1d"):
            stripped["prior_session_1d"] = prior
        return (
            _mark_1d_pending(
                stripped,
                note="盘中 1D 与昨收快照相同，已拒绝陈旧涨跌，待会话源刷新",
            ),
            False,
        )
    out["themes"] = themes_out

    bench = dict(payload.get("bench") or {})
    b1: list[float] = []
    for q in use_quotes.values():
        if q and q.get("change_pct") is not None:
            b1.append(float(q["change_pct"]))
    if b1:
        bench["ret_1d"] = round(sum(b1) / len(b1), 2)
    out["bench"] = bench
    # as_of / updated_bj 仍是请求/计算时刻；1D 数据时刻单独字段
    out["as_of"] = _as_of_iso()
    out["updated_bj"] = now_beijing().strftime("%Y-%m-%d %H:%M")
    qtimes = _quote_data_times(use_quotes)
    out.update(qtimes)
    out.update(_daily_session_close_times(out.get("trade_date")))
    # 第一原则：无源端真实成交印记 → 禁止用墙钟/刷新时刻伪造「最新1D」时间
    if not out.get("data_time_1d_bj") and not out.get("data_time_1d_et"):
        keep = _has_holdable_1d(payload)
        stripped = _strip_stale_1d_fields(out, phase=phase)
        return (
            _mark_1d_pending(
                stripped,
                note="行情无真实成交印记，拒绝用抓取时刻冒充1D时间",
                keep_last_1d=keep,
            ),
            False,
        )
    # 夜盘/盘前/休市：禁止用 rth_fallback（常规收盘）冒充成功 live
    from app.heatmap import _quote_looks_like_rth_close as _rth_close

    if phase in ("overnight", "pre_open", "closed") and (
        qkind == "rth_fallback"
        or _rth_close(
            {
                "quote_time": out.get("data_time_1d_bj"),
                "quote_time_et": out.get("data_time_1d_et"),
            }
        )
    ):
        keep = _has_holdable_1d(payload)
        return (
            _mark_1d_pending(
                _strip_stale_1d_fields(out, phase=phase),
                note="暂无盘后/盘前扩展印记，已拒绝常规收盘04:00冒充最新1D",
                keep_last_1d=keep,
            ),
            False,
        )
    out["data_time_1d_source"] = "live_quote"
    out["live_1d"] = True
    out["1d_fresh"] = True
    out["1d_pending"] = False
    out.pop("1d_hold", None)
    # 源端印记仍滞后：不算成功，清掉假「最新」戳，留给重试
    if phase in ("pre_open", "rth", "settle") and _1d_lag_too_large(out, phase):
        logger.info(
            "mainline 1d overlay lagging stamps phase=%s bj=%s et=%s",
            phase,
            out.get("data_time_1d_bj"),
            out.get("data_time_1d_et"),
        )
        return (
            _mark_1d_pending(
                out, note="行情源印记滞后，正在重拉最新1D…", keep_last_1d=True
            ),
            False,
        )
    if phase in ("overnight", "closed"):
        out["live_1d_note"] = (
            "休市/夜盘无免费 ATS，已用最近盘后价"
            if qkind in ("session", "session+rth_holes")
            else "暂无扩展价，正在重拉"
        )
    elif "rth_stamp_heavy" in str(src) and phase in ("pre_open", "settle"):
        out["live_1d_note"] = "扩展时段会话源偏弱，部分报价仍可能停在常规收盘"
    else:
        out.pop("live_1d_note", None)
    out["quote_count"] = len(use_quotes)
    out["quote_total"] = len(all_symbols())
    out["session_phase"] = phase
    prev_src = payload.get("source") or "snapshot"
    # 去掉旧的 live_1d 后缀再挂新源，避免叠多层
    base_src = str(prev_src).split("+live_1d")[0]
    out["source"] = f"{base_src}+live_1d({src}/{qkind})"
    out["stale"] = False
    note = (payload.get("note") or "").strip()
    for old in (
        "盘中已刷新 1D 报价；5D/20D 与主线判定沿用日线快照。",
        "盘前已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "盘后已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "已刷新最新1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "已刷新最新1D 报价；5D/20D 与主线判定沿用日线快照。",
        "隔夜展示最近盘后/收盘 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "隔夜/周末夜盘已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "隔夜已刷新 1D 夜盘主会话（常规收盘相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "夜盘(ATS)已刷新 1D（相对昨收）；5D/20D 与主线判定沿用日线快照。",
        "暂无 ATS 夜盘源，1D 回退盘后价；配置 TIINGO_API_KEY(BOATS) 后可拉真夜盘。5D/20D 沿用日线快照。",
        "夜盘时段暂无免费 ATS 源，1D 回退盘后价；5D/20D 与主线判定沿用日线快照。",
        "夜盘无免费 ATS 源，1D 用夜盘前最新盘后价；5D/20D 与主线判定沿用日线快照。",
        "夜盘无免费 ATS 源，1D 用当前能拿到的最新盘后价；5D/20D 与主线判定沿用日线快照。",
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


async def _overlay_until_fresh(
    payload: dict[str, Any],
    *,
    phase: str,
    attempts: int = 2,
) -> tuple[dict[str, Any], bool]:
    """首屏前尽量拿到非滞后 1D；失败则 pending，绝不把旧戳当最新。"""
    import asyncio

    last = payload
    ok = False
    for i in range(max(1, attempts)):
        last, ok = await _overlay_live_1d(last, phase=phase)
        if ok:
            return last, True
        if i + 1 < attempts:
            await asyncio.sleep(0.4)
    if not last.get("1d_pending"):
        keep = _has_holdable_1d(last)
        last = _mark_1d_pending(
            _strip_stale_1d_fields(dict(last), phase=phase),
            note=last.get("live_1d_note") or "1D 正在拉取最新报价…",
            keep_last_1d=keep,
        )
    return last, False


def _overlay_bg_busy() -> bool:
    """后台叠加是否仍在跑；超时视为卡死，允许重新调度。"""
    global _OVERLAY_BG, _OVERLAY_BG_STARTED
    if not _OVERLAY_BG:
        return False
    age = time.time() - float(_OVERLAY_BG_STARTED or 0)
    if age > _OVERLAY_BG_STALE_SEC:
        logger.warning(
            "ai_mainline overlay bg stale after %.0fs; allow reschedule", age
        )
        _OVERLAY_BG = False
        _OVERLAY_BG_STARTED = 0.0
        return False
    return True


def _has_ext_live_1d(payload: dict[str, Any] | None, phase: str) -> bool:
    """是否已有可展示的扩展时段 live 1D（非收盘快照/非04:00）。"""
    if not payload or not payload.get("live_1d") or payload.get("1d_pending"):
        return False
    if (payload.get("data_time_1d_source") or "") == "session_close":
        return False
    from app.heatmap import _quote_looks_like_rth_close

    row = {
        "quote_time": payload.get("data_time_1d_bj"),
        "quote_time_et": payload.get("data_time_1d_et"),
    }
    if _is_ext_phase(phase) and _quote_looks_like_rth_close(row):
        return False
    return bool(payload.get("data_time_1d_bj") or payload.get("data_time_1d_et"))


def _schedule_overlay_bg(base: dict[str, Any], phase: str) -> None:
    """1D 叠加放到后台，避免 /api/ai-mainline 卡在慢源上。"""
    import asyncio

    global _OVERLAY_BG, _OVERLAY_BG_STARTED
    if not base or _overlay_bg_busy():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        global _OVERLAY_BG, _OVERLAY_BG_STARTED
        try:
            overlaid, ok = await asyncio.wait_for(
                _overlay_until_fresh(base, phase=phase, attempts=2),
                timeout=55.0,
            )
            _CACHE["data"] = overlaid
            _CACHE["phase"] = phase
            if ok:
                _CACHE["quote_ts"] = time.time()
            else:
                _CACHE["quote_ts"] = 0.0
        except Exception:
            logger.exception("ai_mainline overlay background failed")
        finally:
            _OVERLAY_BG = False
            _OVERLAY_BG_STARTED = 0.0

    _OVERLAY_BG = True
    _OVERLAY_BG_STARTED = time.time()
    loop.create_task(_run())


async def _ensure_live_1d(
    payload: dict[str, Any], phase: str, *, sync_timeout: float = 12.0
) -> dict[str, Any]:
    """缺扩展 live 时短同步叠一层；超时立刻 pending+后台，禁止把整表卡死。"""
    import asyncio

    if not _live_1d_active(phase):
        return payload
    if _has_ext_live_1d(payload, phase):
        return payload
    # 已有后台在跑：勿再同步堵请求，先标 pending 让前端快轮询
    if _overlay_bg_busy():
        keep = _has_holdable_1d(payload)
        return _mark_1d_pending(
            _strip_stale_1d_fields(dict(payload), phase=phase),
            note="1D 扩展行情后台拉取中…",
            keep_last_1d=keep,
        )
    try:
        overlaid, ok = await asyncio.wait_for(
            _overlay_until_fresh(payload, phase=phase, attempts=1),
            timeout=sync_timeout,
        )
        if ok:
            _CACHE["quote_ts"] = time.time()
            return overlaid
        _CACHE["quote_ts"] = 0.0
        _schedule_overlay_bg(overlaid, phase)
        return overlaid
    except Exception as exc:
        logger.info("ai_mainline sync 1d overlay: %s", type(exc).__name__)
        keep = _has_holdable_1d(payload)
        pending = _mark_1d_pending(
            _strip_stale_1d_fields(dict(payload), phase=phase),
            note="1D 扩展行情拉取超时，正在后台重试",
            keep_last_1d=keep,
        )
        _CACHE["quote_ts"] = 0.0
        _schedule_overlay_bg(pending, phase)
        return pending


def _schedule_full_refresh_bg() -> None:
    """日K全量刷新放到后台，页面先用库内/缓存快照。"""
    import asyncio

    global _FULL_BG
    if _FULL_BG:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        global _FULL_BG
        try:
            await compute_mainline(force=True)
        except Exception:
            logger.exception("ai_mainline full refresh background failed")
        finally:
            _FULL_BG = False

    _FULL_BG = True
    loop.create_task(_run())


def _theme_name_map() -> dict[str, str]:
    return {t["key"]: t.get("name") or t["key"] for t in enabled_themes()}


def _quote_data_times(quotes: dict[str, dict[str, Any]]) -> dict[str, str | None]:
    """从行情行取「数据本身」最新时刻（按解析后的时间，不是字符串 max）。"""
    best: dict[str, Any] | None = None
    best_dt: datetime | None = None
    for q in quotes.values():
        if not q:
            continue
        dt = _parse_member_quote_dt(q)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best = q
    if best:
        return {
            "data_time_1d_bj": best.get("quote_time") or None,
            "data_time_1d_et": best.get("quote_time_et") or None,
        }
    # 解析失败时退回 ISO 北京时间字符串排序
    bj_times = [
        str(q["quote_time"]) for q in quotes.values() if q and q.get("quote_time")
    ]
    et_times = [
        str(q["quote_time_et"])
        for q in quotes.values()
        if q and q.get("quote_time_et") and re.match(r"\d{4}-", str(q["quote_time_et"]))
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


def _payload_from_db_snapshots_uncached() -> dict[str, Any] | None:
    """用库内已落库的主线日快照组装页面（真实历史，非估算）。"""
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


def _payload_from_db_snapshots() -> dict[str, Any] | None:
    """带 TTL + 超时的库快照；慢库时回退上一份缓存，勿拖死整表。"""
    now = time.time()
    age = now - float(_DB_SNAP_CACHE.get("ts") or 0)
    cached = _DB_SNAP_CACHE.get("data")
    if cached is not None and age < _DB_SNAP_CACHE_TTL_SEC:
        return cached
    fresh = _run_db_budget(
        _payload_from_db_snapshots_uncached, label="db_snapshots"
    )
    if fresh is not None:
        _DB_SNAP_CACHE["data"] = fresh
        _DB_SNAP_CACHE["ts"] = now
        return fresh
    return cached


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

    def _streak_job() -> dict[str, int]:
        with SessionLocal() as db:
            return _load_streak_before_today(db, today)

    loaded = _run_db_budget(_streak_job, budget=3.0, label="streak")
    if loaded is not None:
        prior_streak = loaded

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

    if not force:
        ttl = _cache_ttl_sec(phase, cached)
        cache_age = now - float(_CACHE.get("ts") or 0)
        # 热路径：有内存缓存时绝不先打 Turso（慢库曾把每次轮询拖到数分钟）
        if cached and cache_age < ttl and not _needs_full_refresh(cached):
            quote_age = now - float(_CACHE.get("quote_ts") or 0)
            need_overlay, reason = _needs_1d_refresh(
                cached, phase, quote_age=quote_age
            )
            out = cached
            if need_overlay:
                logger.info(
                    "ai_mainline 1d refresh phase=%s reason=%s quote_age=%.0fs",
                    phase,
                    reason,
                    quote_age,
                )
                if not _has_ext_live_1d(cached, phase):
                    out = await _ensure_live_1d(cached, phase)
                    _CACHE["data"] = out
                    _CACHE["phase"] = phase
                else:
                    _schedule_overlay_bg(cached, phase)
            return _finalize_payload(out)

        db_payload = _payload_from_db_snapshots()
        base = _pick_best_base(cached, db_payload)
        needs_full = _needs_full_refresh(base)

        if not needs_full and base is not None:
            out = base
            if _live_1d_active(phase):
                need, reason = _needs_1d_refresh(base, phase, quote_age=1e9)
                if need:
                    logger.info(
                        "ai_mainline 1d refresh from base phase=%s reason=%s",
                        phase,
                        reason,
                    )
                    out = await _ensure_live_1d(base, phase)
            elif out is db_payload and out is not None:
                out = dict(out)
                out["note"] = (out.get("note") or "") or (
                    "展示库内主线日线快照（主线按日变化，未强制重拉外网）。"
                )
            _CACHE["ts"] = now
            _CACHE["data"] = out
            _CACHE["phase"] = phase
            return _finalize_payload(out)
        logger.info(
            "ai_mainline full refresh phase=%s snap=%s cov=%s",
            phase,
            _payload_trade_date(base),
            _period_coverage(base),
        )
        if not force and base and _period_coverage(base) >= _COV_OK:
            _schedule_full_refresh_bg()
            out = base
            if _live_1d_active(phase):
                out = await _ensure_live_1d(base, phase)
            _CACHE["data"] = out
            _CACHE["ts"] = now
            _CACHE["phase"] = phase
            return _finalize_payload(out)
        if not force and base is None and cached is None:
            # Turso 超时且无内存缓存：禁止掉进 100s+ 全量重算把整表卡死
            logger.warning("ai_mainline no snapshot (db slow); defer full compute to background")
            _schedule_full_refresh_bg()
            return {
                "success": False,
                "enabled": True,
                "as_of": _as_of_iso(),
                "themes": [],
                "primary": None,
                "secondary": None,
                "status": "error",
                "summary": "主线快照库暂慢/不可用，已后台重试，请稍后刷新。",
                "note": "db_timeout",
                "1d_pending": True,
                "session_phase": phase,
            }
    else:
        db_payload = _payload_from_db_snapshots()
        base = _pick_best_base(cached, db_payload)

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
        payload, ok = await _overlay_until_fresh(payload, phase=phase, attempts=2)
        if ok:
            _CACHE["quote_ts"] = now
        else:
            _CACHE["quote_ts"] = 0.0
    else:
        _CACHE["quote_ts"] = now
    _CACHE["data"] = payload
    _CACHE["phase"] = phase
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


def _run_db_budget(fn, *, budget: float = _DB_QUERY_BUDGET_SEC, label: str = "db"):
    """限时跑同步 DB；超时返回 None，避免卡住 API。

    注意：不可用 ``with ThreadPoolExecutor`` 默认 shutdown(wait=True)，
    否则 Turso 挂起时「4s 超时」仍会再等线程结束，整表卡数分钟。
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(fn).result(timeout=budget)
    except FuturesTimeout:
        logger.warning("ai_mainline %s timed out after %.1fs", label, budget)
        return None
    except Exception:
        logger.exception("ai_mainline %s failed", label)
        return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


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


def history_pulse_1d(days: int = 10) -> list[dict[str, Any]]:
    """逐日短线：广度=1 且 1D≥2% 中取最强（有效成分≥3）。"""
    from app.database import AiMainlineDailySnapshot, SessionLocal

    names = _theme_name_map()
    since = _today_et() - timedelta(days=days)
    with SessionLocal() as db:
        rows = (
            db.query(AiMainlineDailySnapshot)
            .filter(
                AiMainlineDailySnapshot.theme_key != META_KEY,
                AiMainlineDailySnapshot.trade_date >= since,
            )
            .order_by(AiMainlineDailySnapshot.trade_date.asc())
            .all()
        )
    by_date: dict[date, list[Any]] = {}
    for r in rows:
        by_date.setdefault(r.trade_date, []).append(r)
    out: list[dict[str, Any]] = []
    for td in sorted(by_date):
        best = None
        for r in by_date[td]:
            cand = {
                "ret_1d": r.ret_1d,
                "n_valid": r.n_valid,
                "breadth": r.breadth,
            }
            if not _short_rotation_eligible(cand):
                continue
            if best is None or float(r.ret_1d) > float(best.ret_1d):
                best = r
        if not best:
            out.append(_empty_short_rotation_day(td.isoformat()))
            continue
        out.append(
            {
                "trade_date": td.isoformat(),
                "primary_key": best.theme_key,
                "primary_name": names.get(best.theme_key, best.theme_key),
                "status": "pulse",
                "ret_1d": round(float(best.ret_1d), 2),
                "breadth": best.breadth,
                "n_valid": best.n_valid,
            }
        )
    return out


def _cached_history_primary(days: int = 14) -> list[dict[str, Any]]:
    now = time.time()
    age = now - float(_ROT_CACHE.get("primary_ts") or 0)
    cached = list(_ROT_CACHE.get("primary") or [])
    if cached and age < _ROT_CACHE_TTL_SEC:
        return cached
    fresh = _run_db_budget(lambda: history_primary(days), label="history_primary")
    if fresh is not None:
        _ROT_CACHE["primary"] = fresh
        _ROT_CACHE["primary_ts"] = now
        return fresh
    return cached


def _cached_history_pulse_1d(days: int = 10) -> list[dict[str, Any]]:
    now = time.time()
    age = now - float(_ROT_CACHE.get("pulse_ts") or 0)
    cached = list(_ROT_CACHE.get("pulse") or [])
    if cached and age < _ROT_CACHE_TTL_SEC:
        return cached
    fresh = _run_db_budget(lambda: history_pulse_1d(days), label="history_pulse_1d")
    if fresh is not None:
        _ROT_CACHE["pulse"] = fresh
        _ROT_CACHE["pulse_ts"] = now
        return fresh
    return cached


def _pulse_history_with_today(
    payload: dict[str, Any], *, days: int = 10, use_cache: bool = False
) -> list[dict[str, Any]]:
    """历史日快照短线 + 当日实时短线（须广度=1 且 1D≥2%；有则覆盖同日）。"""
    hist = (
        _cached_history_pulse_1d(days) if use_cache else history_pulse_1d(days)
    )
    pulse = payload.get("pulse_1d") or {}
    if payload.get("1d_pending") or not pulse.get("key"):
        return hist
    today_iso = _today_et().isoformat()
    snap_td = str(payload.get("trade_date") or "")[:10]
    # 盘中用美东今日；收盘后快照日与今日可能同一交易日
    overlay_td = today_iso if _live_1d_active() else (snap_td or today_iso)
    if _short_rotation_eligible(pulse):
        row = {
            "trade_date": overlay_td,
            "primary_key": pulse.get("key"),
            "primary_name": pulse.get("name") or pulse.get("key"),
            "status": "pulse",
            "ret_1d": pulse.get("ret_1d"),
            "breadth": pulse.get("breadth"),
            "n_valid": pulse.get("n_valid"),
            "n_up": pulse.get("n_up"),
        }
    else:
        row = _empty_short_rotation_day(overlay_td)
    hist = [h for h in hist if str(h.get("trade_date") or "")[:10] != overlay_td]
    hist.append(row)
    hist.sort(key=lambda h: str(h.get("trade_date") or ""))
    return hist
