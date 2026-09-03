"""同日合并推送文案（纯文本）。"""

from __future__ import annotations

from datetime import date

from app.earnings_monitor.config import SECTOR_LABELS
from app.earnings_monitor.trade_window import compute_trade_window, session_label


def _localize_one_liner(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for code, label in SECTOR_LABELS.items():
        out = out.replace(code, label)
    return out


def build_earnings_batch_push(
    earnings_date: date,
    events: list,
) -> tuple[str, str]:
    """events 已按 score 降序。"""
    n = len(events)
    sample = events[0]
    tw0 = compute_trade_window(sample.earnings_date, sample.session or "TBD")
    # 标题/导语用北京揭晓时刻为主，美东日期仅旁注
    mmdd_bj = (tw0.earnings_release_bj or "")[:5] or earnings_date.strftime("%m-%d")
    if n == 1:
        e = events[0]
        title = (
            f"【AI财报 T-2】{e.ticker} {mmdd_bj}北京 "
            f"{session_label(e.session)} 得分 {e.score_total or '—'}"
        )
    else:
        tickers = " / ".join(e.ticker for e in events[:6])
        more = f" 等{n}家" if n > 6 else ""
        title = f"【AI财报 T-2】{mmdd_bj}北京 共 {n} 家：{tickers}{more}"

    lines = [
        "【AI财报提醒】",
        "",
        f"以下公司预计北京 {tw0.earnings_release_bj} 前后发布财报"
        f"（美东日历日 {earnings_date.isoformat()}，提前 2 天提醒）。",
        "策略：财报发布后买，约 2 个交易日内卖完；财报前勿埋伏。",
        "说明：非投资建议；beat 不代表必涨；跳空≥15%不追。",
        "",
    ]
    for i, e in enumerate(events, start=1):
        tw = compute_trade_window(e.earnings_date, e.session or "TBD")
        lines.append(
            f"{i}. {e.ticker} {e.company_name} | 得分 {e.score_total or '—'}/100 | "
            f"{session_label(e.session)}"
        )
        one = _localize_one_liner(e.one_liner)
        if one:
            lines.append(f"理由：{one}")
        lines.append(f"买入：{tw.buy_window_bj} 北京 · {tw.buy_window_et} 美东")
        lines.append(f"卖出首选：{tw.sell_window_bj} 北京 · {tw.sell_window_et} 美东")
        lines.append(
            f"卖出最晚：{tw.sell_deadline_bj} 北京 · {tw.sell_deadline_et} 美东"
        )
        if e.risk_oneliner:
            lines.append(f"风险：{e.risk_oneliner}")
        lines.append("")

    lines.append("详情见网站：AI 产业链合作快讯 → 财报日历")
    return title, "\n".join(lines).rstrip()
