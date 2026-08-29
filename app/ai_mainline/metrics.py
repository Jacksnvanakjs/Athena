"""等权收益、相对基准、breadth。"""

from __future__ import annotations

from typing import Any


def _mean(vals: list[float]) -> float | None:
    if not vals:
        return None
    return round(sum(vals) / len(vals), 2)


def theme_metrics(
    theme: dict[str, Any],
    quotes: dict[str, dict[str, Any]],
    period: dict[str, dict[str, float | None]],
) -> dict[str, Any]:
    """计算单子线等权指标。缺行情成分剔除。"""
    ret_1d_list: list[float] = []
    ret_5d_list: list[float] = []
    ret_20d_list: list[float] = []
    up = 0
    quoted: list[tuple[str, float]] = []
    members: list[dict[str, Any]] = []

    for t in theme.get("tickers") or []:
        sym = (t.get("symbol") or "").upper().strip()
        if not sym:
            continue
        name = (t.get("name") or "").strip()
        q = quotes.get(sym)
        if q and q.get("change_pct") is not None:
            chg = float(q["change_pct"])
            ret_1d_list.append(chg)
            if chg > 0:
                up += 1
            quoted.append((sym, chg))
            members.append({"symbol": sym, "name": name, "ret_1d": round(chg, 2)})
            p = period.get(sym) or {}
            if p.get("ret_5d") is not None:
                ret_5d_list.append(float(p["ret_5d"]))
            if p.get("ret_20d") is not None:
                ret_20d_list.append(float(p["ret_20d"]))
        else:
            members.append({"symbol": sym, "name": name, "ret_1d": None})

    n_valid = len(ret_1d_list)
    breadth = round(up / n_valid, 4) if n_valid else None
    quoted.sort(key=lambda x: x[1], reverse=True)
    # 有行情的按涨跌排序，无行情的跟在后面
    members.sort(
        key=lambda m: (
            m.get("ret_1d") is None,
            -(m["ret_1d"] if m.get("ret_1d") is not None else 0.0),
        )
    )

    return {
        "key": theme.get("key"),
        "name": theme.get("name"),
        "heatmap_theme_key": theme.get("heatmap_theme_key"),
        "ret_1d": _mean(ret_1d_list),
        "ret_5d": _mean(ret_5d_list),
        "ret_20d": _mean(ret_20d_list),
        "breadth": breadth,
        "n_valid": n_valid,
        "n_up": up,
        "leaders": [s for s, _ in quoted[:3]],
        "members": members,
        "tickers": [m["symbol"] for m in members],
        "ticker_count": len(members),
    }


def attach_relative(
    themes: list[dict[str, Any]],
    bench: dict[str, float | None],
) -> list[dict[str, Any]]:
    """写入 rel_* = theme - bench。"""
    out: list[dict[str, Any]] = []
    for t in themes:
        row = dict(t)
        for horizon in ("1d", "5d", "20d"):
            tr = t.get(f"ret_{horizon}")
            br = bench.get(f"ret_{horizon}")
            if tr is None or br is None:
                row[f"rel_{horizon}"] = None
            else:
                row[f"rel_{horizon}"] = round(float(tr) - float(br), 2)
        out.append(row)
    return out


def compute_benchmark(
    themes: list[dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    period: dict[str, dict[str, float | None]],
) -> dict[str, Any]:
    """ai_bench = 全部 enabled 子线成分去重后等权。"""
    seen: set[str] = set()
    ret_1d_list: list[float] = []
    ret_5d_list: list[float] = []
    ret_20d_list: list[float] = []
    for theme in themes:
        for t in theme.get("tickers") or []:
            sym = (t.get("symbol") or "").upper().strip()
            if not sym or sym in seen:
                continue
            seen.add(sym)
            q = quotes.get(sym)
            if q and q.get("change_pct") is not None:
                ret_1d_list.append(float(q["change_pct"]))
            p = period.get(sym) or {}
            if p.get("ret_5d") is not None:
                ret_5d_list.append(float(p["ret_5d"]))
            if p.get("ret_20d") is not None:
                ret_20d_list.append(float(p["ret_20d"]))
    return {
        "key": "ai_bench",
        "name": "AI 综合基准",
        "ret_1d": _mean(ret_1d_list),
        "ret_5d": _mean(ret_5d_list),
        "ret_20d": _mean(ret_20d_list),
        "n_valid": len(ret_1d_list),
    }
