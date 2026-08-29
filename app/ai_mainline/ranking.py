"""排名与主线确认（confirmed / emerging / no_mainline）。"""

from __future__ import annotations

from typing import Any

from app.ai_mainline.config import (
    AI_MAINLINE_CONFIRM_DAYS,
    AI_MAINLINE_MIN_BREADTH,
    AI_MAINLINE_MIN_REL_5D,
    AI_MAINLINE_MIN_VALID,
)


def rank_themes(themes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 rel_5d 降序排名；无 rel_5d 的排最后。"""
    ranked = sorted(
        themes,
        key=lambda t: (
            t.get("rel_5d") is None,
            -(t.get("rel_5d") if t.get("rel_5d") is not None else 0.0),
        ),
    )
    out: list[dict[str, Any]] = []
    rank = 0
    for t in ranked:
        row = dict(t)
        if t.get("n_valid", 0) < AI_MAINLINE_MIN_VALID or t.get("rel_5d") is None:
            row["rank_5d"] = None
        else:
            rank += 1
            row["rank_5d"] = rank
        out.append(row)
    return out


def _eligible(t: dict[str, Any]) -> bool:
    return (
        (t.get("n_valid") or 0) >= AI_MAINLINE_MIN_VALID
        and t.get("rel_5d") is not None
        and t.get("rank_5d") is not None
    )


def _meets_primary_gates(t: dict[str, Any]) -> bool:
    rel = t.get("rel_5d")
    breadth = t.get("breadth")
    if rel is None:
        return False
    if float(rel) < AI_MAINLINE_MIN_REL_5D:
        return False
    if breadth is None or float(breadth) < AI_MAINLINE_MIN_BREADTH:
        return False
    return True


def _top2_and_positive(t: dict[str, Any]) -> bool:
    rank = t.get("rank_5d")
    rel = t.get("rel_5d")
    return rank is not None and rank <= 2 and rel is not None and float(rel) > 0


def judge_mainline(
    themes: list[dict[str, Any]],
    streak_by_key: dict[str, int] | None = None,
) -> dict[str, Any]:
    """
    产出 primary / secondary / status / summary。
    streak_by_key: 各子线「连续进入 Top2 且 rel_5d>0」的交易日数（含今日）。
    """
    streak_by_key = streak_by_key or {}
    eligible = [t for t in themes if _eligible(t)]
    if not eligible:
        return {
            "primary": None,
            "secondary": None,
            "status": "no_mainline",
            "streak_days": 0,
            "summary": "暂无明确主线：有效子线不足。相对强弱判断，非互斥；不构成投资建议。",
        }

    # 若没有任何子线达到相对门槛 → no_mainline
    if not any(
        (t.get("rel_5d") is not None and float(t["rel_5d"]) >= AI_MAINLINE_MIN_REL_5D)
        for t in eligible
    ):
        return {
            "primary": None,
            "secondary": None,
            "status": "no_mainline",
            "streak_days": 0,
            "summary": "暂无明确主线（宏观/共振或相对强弱均未达门槛）。相对强弱判断，非互斥；不构成投资建议。",
        }

    top = sorted(
        eligible,
        key=lambda t: float(t.get("rel_5d") or -999),
        reverse=True,
    )
    cand = top[0]
    secondary_raw = top[1] if len(top) > 1 and float(top[1].get("rel_5d") or -1) > 0 else None

    primary: dict[str, Any] | None = None
    status = "no_mainline"
    streak = 0

    if _meets_primary_gates(cand):
        key = cand["key"]
        # 今日若进入 Top2+rel>0，streak 至少为传入值（pipeline 已含今日）
        streak = int(streak_by_key.get(key) or (1 if _top2_and_positive(cand) else 0))
        if streak >= AI_MAINLINE_CONFIRM_DAYS and _top2_and_positive(cand):
            status = "confirmed"
        else:
            status = "emerging"
        primary = {
            "key": cand["key"],
            "name": cand["name"],
            "status": status,
            "streak_days": streak,
            "ret_5d": cand.get("ret_5d"),
            "rel_5d": cand.get("rel_5d"),
            "ret_1d": cand.get("ret_1d"),
            "breadth": cand.get("breadth"),
            "n_valid": cand.get("n_valid"),
            "n_up": cand.get("n_up"),
        }
    else:
        # Top1 未过门槛：仍可标 emerging 观察，但不作为正式主线
        if float(cand.get("rel_5d") or 0) > 0:
            status = "emerging"
            streak = int(streak_by_key.get(cand["key"]) or 0)
            primary = {
                "key": cand["key"],
                "name": cand["name"],
                "status": status,
                "streak_days": streak,
                "ret_5d": cand.get("ret_5d"),
                "rel_5d": cand.get("rel_5d"),
                "ret_1d": cand.get("ret_1d"),
                "breadth": cand.get("breadth"),
                "n_valid": cand.get("n_valid"),
                "n_up": cand.get("n_up"),
            }
        else:
            status = "no_mainline"

    secondary = None
    if secondary_raw and (not primary or secondary_raw["key"] != primary.get("key")):
        secondary = {
            "key": secondary_raw["key"],
            "name": secondary_raw["name"],
            "ret_5d": secondary_raw.get("ret_5d"),
            "rel_5d": secondary_raw.get("rel_5d"),
        }

    summary = _build_summary(primary, secondary, status)
    return {
        "primary": primary,
        "secondary": secondary,
        "status": status if primary else "no_mainline",
        "streak_days": streak,
        "summary": summary,
    }


def _build_summary(
    primary: dict[str, Any] | None,
    secondary: dict[str, Any] | None,
    status: str,
) -> str:
    if not primary or status == "no_mainline":
        return "暂无明确主线（宏观/共振下跌或普涨）。相对强弱判断，非互斥；不构成投资建议。"
    st_label = "已确认" if primary.get("status") == "confirmed" else "观察中"
    rel = primary.get("rel_5d")
    ret = primary.get("ret_5d")
    breadth = primary.get("breadth")
    n_up = primary.get("n_up")
    n_valid = primary.get("n_valid")
    breadth_txt = ""
    if breadth is not None:
        if n_up is not None and n_valid:
            breadth_txt = f"｜上涨 {n_up}/{n_valid}"
        else:
            breadth_txt = f"｜上涨占比 {breadth:.0%}"
    sec_name = secondary["name"] if secondary else "无"
    rel_s = f"{rel:+.1f}%" if rel is not None else "—"
    ret_s = f"{ret:+.1f}%" if ret is not None else "—"
    return (
        f"当前主线：{primary['name']}（{st_label}）\n"
        f"近5日相对 AI 基准：{rel_s}｜板块 {ret_s}{breadth_txt}\n"
        f"次强：{sec_name}\n"
        f"说明：相对强弱判断，非互斥；不构成投资建议。"
    )
