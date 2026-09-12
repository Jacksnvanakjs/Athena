"""手机推送正文：纯文本，避免 HTML 标签在微信里显示成代码。"""

from __future__ import annotations

from app.deal_monitor.headline_zh import build_zh_headline, needs_zh_headline


def _party(name: str | None, ticker: str | None) -> str:
    n = (name or "").strip() or "—"
    t = (ticker or "").strip()
    return f"{n} ({t})" if t else n


def _zh_core_message(event) -> str:
    """一句提炼过的中文核心（不用英文原文摘要）。"""
    hl = (getattr(event, "headline", None) or "").strip()
    ticker = (getattr(event, "beneficiary_ticker", None) or "").strip()
    summary = getattr(event, "summary", None)
    if hl and not needs_zh_headline(hl):
        return hl
    return build_zh_headline(ticker, hl, summary)


def build_deal_push_content(event) -> tuple[str, str]:
    anchor_ticker = (event.anchor_ticker or "").strip() or "未上市"
    title = f"[AI合作] {event.beneficiary_ticker} ← {event.anchor_name}"

    lines = [
        "【AI 产业链合作快讯】",
        "",
        f"受益：{_party(event.beneficiary_name, event.beneficiary_ticker)}",
        f"锚点：{_party(event.anchor_name, anchor_ticker)}",
        "",
        _zh_core_message(event),
    ]
    source_url = (event.source_url or "").strip()
    if source_url:
        lines.extend(["", f"原文：{source_url}"])

    return title, "\n".join(lines)


def build_deal_digest_push_content(events: list) -> tuple[str, str]:
    """多条积压快讯合并成一条，避免额度恢复后连发刷屏。"""
    items = [e for e in events if e is not None]
    n = len(items)
    if n == 0:
        return "[AI合作] 综合", "（无条目）"
    if n == 1:
        return build_deal_push_content(items[0])

    tickers: list[str] = []
    seen: set[str] = set()
    for e in items:
        t = (e.beneficiary_ticker or "").strip()
        if t and t not in seen:
            seen.add(t)
            tickers.append(t)
    head = "、".join(tickers[:5])
    if len(tickers) > 5:
        head += f" 等{len(tickers)}只"
    title = f"[AI合作·综合] {n}条 · {head}"

    lines = [
        f"【AI 合作快讯综合】共 {n} 条（积压合并推送，免连发）",
        "",
    ]
    for i, e in enumerate(items, 1):
        anchor = (e.anchor_ticker or "").strip() or (e.anchor_name or "—")
        lines.append(
            f"{i}. {(e.beneficiary_ticker or '—')} ← {anchor}"
        )
        core = _zh_core_message(e)
        if core:
            short = core[:120] + ("…" if len(core) > 120 else "")
            lines.append(f"   {short}")
        url = (e.source_url or "").strip()
        if url:
            lines.append(f"   {url}")
        lines.append("")

    return title, "\n".join(lines).rstrip()


def build_nvda_push_content(event) -> tuple[str, str]:
    tag = "A+B" if event.signal_tier == "A_PLUS_B" else "A"
    observe = "" if event.buy_ok else "·观察"
    title = f"[黄仁勋] NVDA {tag}档{observe} · {event.beneficiary_ticker}"

    lines = [
        "【黄仁勋 / NVDA 产业动作】",
        "",
        f"标的：{_party(event.beneficiary_name, event.beneficiary_ticker)}",
        "",
        _zh_core_message(event),
    ]
    if event.buy_window or event.sell_window:
        lines.append("")
        if event.buy_window:
            lines.append(f"买入窗口：{event.buy_window}")
        if event.sell_window:
            lines.append(f"卖出窗口：{event.sell_window}")
    source_url = (event.source_url or "").strip()
    if source_url:
        lines.extend(["", f"原文：{source_url}"])

    return title, "\n".join(lines)


# 财报合并推送见 app.earnings_monitor.push.build_earnings_batch_push
