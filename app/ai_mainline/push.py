"""主线切换推送（默认关闭）。"""

from __future__ import annotations

from typing import Any


def build_mainline_switch_push(
    old_name: str | None,
    new_primary: dict[str, Any],
    themes: list[dict[str, Any]],
    summary: str,
) -> tuple[str, str]:
    old = old_name or "无"
    new = new_primary.get("name") or new_primary.get("key") or "?"
    title = f"【AI 主线切换】{old} → {new}"
    top3 = sorted(
        [t for t in themes if t.get("rel_5d") is not None],
        key=lambda t: float(t["rel_5d"]),
        reverse=True,
    )[:3]
    lines = [
        title,
        "",
        summary.strip(),
        "",
        "Top3 相对5日：",
    ]
    for i, t in enumerate(top3, start=1):
        lines.append(f"{i}. {t.get('name')} rel_5d {t.get('rel_5d'):+.1f}%")
    lines.append("")
    lines.append("免责声明：相对强弱研究用途，非投资建议。")
    lines.append("详情见网站：AI 产业链合作快讯 → AI 主线")
    return title, "\n".join(lines)
