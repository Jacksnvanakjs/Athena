"""把外文新闻标题提炼成「代码：简短中文」展示标题。"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_CJK = re.compile(r"[\u4e00-\u9fff]")
_JP_KANA = re.compile(r"[\u3040-\u30ff]")
_KO = re.compile(r"[\uac00-\ud7af]")
_HTML = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SITE_SUFFIX = re.compile(
    r"\s*[-–—|]\s*(?:EnergyNow\.com|Stocktwits|Yahoo|Reuters|Bloomberg|CNBC|"
    r"Business Wire|PR Newswire|GlobeNewswire|Seeking Alpha).*$",
    re.I,
)
_ZH_STYLE = re.compile(r"^[A-Za-z0-9.\-]{1,8}：.{0,80}$")


def strip_html(text: str) -> str:
    return _WS.sub(" ", _HTML.sub(" ", text or "")).strip()


def has_chinese(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def needs_zh_headline(headline: str) -> bool:
    """无简体中文展示说明则需重写（日文/韩文/纯外文都要换）。"""
    h = (headline or "").strip()
    if not h:
        return True
    if _JP_KANA.search(h) or _KO.search(h):
        return True
    # 已是「代码：…」且含中文
    if _ZH_STYLE.match(h) and len(_CJK.findall(h)) >= 2:
        return False
    if len(_CJK.findall(h)) >= 4 and "：" in h:
        return False
    return True


def _amount_cn(text: str) -> str | None:
    m = re.search(
        r"\$?\s*([\d,.]+)\s*(billion|million|bn|b\b|m\b)",
        text,
        re.I,
    )
    if not m:
        return None
    num = m.group(1).replace(",", "")
    unit = m.group(2).lower()
    try:
        val = float(num)
    except ValueError:
        return None
    if unit.startswith("b"):
        if val >= 10:
            return f"约{val:g}亿美元"
        return f"约{val:g}亿美元" if val >= 1 else f"约{int(val * 10)}亿美元"
    # million
    if val >= 1000:
        return f"约{val / 1000:g}亿美元"
    return f"约{val:g}万美元"


def _party_hint(text: str) -> str | None:
    parties = [
        ("OpenAI", "OpenAI"),
        ("Anthropic", "Anthropic"),
        ("Google Cloud", "谷歌云"),
        ("Google", "谷歌"),
        ("Microsoft", "微软"),
        ("Amazon", "亚马逊"),
        ("AWS", "AWS"),
        ("NVIDIA", "英伟达"),
        ("Nvidia", "英伟达"),
        ("Salesforce", "Salesforce"),
        ("Oracle", "Oracle"),
        ("Meta", "Meta"),
        ("Claude", "Claude"),
        ("Agentforce", "Agentforce"),
        ("Claudeforce", "Claudeforce"),
    ]
    found: list[str] = []
    low = text
    for en, zh in parties:
        if re.search(rf"\b{re.escape(en)}\b", low, re.I) and zh not in found:
            found.append(zh)
        if len(found) >= 2:
            break
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    return f"{found[0]}×{found[1]}"


def _brief_from_text(text: str, ticker: str) -> str | None:
    t = strip_html(text)
    t = _SITE_SUFFIX.sub("", t)
    low = t.lower()
    amt = _amount_cn(t)
    party = _party_hint(t)

    # 命名产品优先（避免 Claudeforce 文里的 Agentforce 抢匹配）
    if re.search(r"\bclaudeforce\b", low):
        return "Salesforce×Anthropic 推出 Claudeforce"
    if "wayfinder" in low or "frontier ai services" in low:
        return "扩展 Wayfinder Frontier AI 服务"
    if re.search(r"\bagentforce\b", low) and "claudeforce" not in low:
        return "Smarsh 规模化 Agentforce" if "smarsh" in low else (
            f"{party or '伙伴'}规模化 Agentforce" if "scale" in low else "扩展 Agentforce 能力"
        )

    # 融资（正文常夹带 NVIDIA，勿判成合作）
    if re.search(
        r"\b(?:ai\s+factory\s+financ|private\s+credit\s+financ|financ(?:e|ing)\s+for|"
        r"raises?\s+\$|raised\s+\$|credit\s+facility|blue\s+owl)\b",
        low,
    ):
        return f"AI 工厂/算力融资（{amt}）" if amt else "AI 相关融资"

    # 并购出售
    m = re.search(
        r"(?:announces?\s+)?sale\s+to\s+([A-Za-z0-9 .,&\-]+?)\s+for\s+(\$[\d,.]+\s*(?:billion|million))",
        t,
        re.I,
    )
    if m:
        buyer = m.group(1).strip().rstrip(",")
        if ticker.upper() in buyer.upper() or "flex" in buyer.lower():
            return f"收购 EPC Power（{amt or '大额'}）" if "epc" in low else f"收购交易（{amt or '大额'}）"
        return f"出售给 {buyer[:20]}（{amt or '大额'}）"

    if re.search(r"\b(?:acquires?|acquired|acquisition|to\s+acquire)\b", low):
        target = party or "标的"
        return f"收购相关交易（{amt}）" if amt else f"收购/并购相关（{target}）"

    # 电力 / PPA / 储能
    if re.search(r"\b(?:power\s+deal|ppa|power\s+purchase|用电|geothermal)\b", low):
        years = re.search(r"(\d+)\s*-?\s*year", low)
        y = f"{years.group(1)}年" if years else ""
        who = party or "合作方"
        return f"与{who}签{y}电力协议" if y else f"与{who}电力/购电合作"

    if re.search(r"\b(?:bess|battery\s+energy|storage|储能)\b", low) and re.search(
        r"\b(?:mw|gwh|megawatt|construction|constructie)\b", low
    ):
        mw = re.search(r"(\d+)\s*mw", low)
        return f"储能项目推进" + (f"（{mw.group(1)}MW）" if mw else "")

    # 管道 / 数据中心配套
    if re.search(r"\blateral\s+pipelines?\b|\bdata\s+center\s+exchang", low):
        return "数据中心配套管线扩张"

    if re.search(r"\bagentic\s+commerce\b", low) or (
        "similarweb" in low and ("niq" in low or "commerce" in low)
    ):
        return "与 NIQ 推进 Agentic 电商测量"

    # GPU 部署
    if re.search(r"\b(?:deployment|deploys?|deployed)\b", low) and re.search(
        r"\b(?:gpu|blackwell|instinct|rtx)\b", low
    ):
        n = re.search(r"(\d+)\s+(?:nvidia\s+)?(?:rtx|gpu|blackwell)", low)
        chip = "Blackwell GPU" if "blackwell" in low else ("Instinct GPU" if "instinct" in low else "GPU")
        who = "为 Runpod " if "runpod" in low else ""
        return f"{who}部署" + (f"{n.group(1)} 块 " if n else "") + chip

    # 市场上架 / 产品接入
    if re.search(r"\bmarketplace\b", low):
        return f"{party or '平台'}应用市场上架" if party else "AI 应用市场上架"

    if re.search(r"\bbring(?:s|ing)?\b.{0,40}\b(?:openai|gpt|claude|model)", low):
        return f"接入 {party or '大模型'} 安全/业务能力"

    # AWS Transform / 云平台扩展
    if re.search(r"\baws\s+transform\b|\bexpands?\s+aws\b", low):
        return "扩展 AWS Transform / 云迁移支持"

    # 合作 / 伙伴
    if re.search(r"\b(?:partners?(?:\s+with)?|partnership|collaborat\w*|agreement)\b", low):
        if "vulnerability" in low or "security" in low:
            return f"与{party or '伙伴'}合作网络安全"
        if party:
            return f"与{party}深化 AI 协作"
        return "宣布 AI 相关合作"

    if re.search(r"\b8-k\b", low):
        return "提交 8-K 重大协议公告"

    if party:
        return f"与{party}相关 AI 动态"
    return None


def build_zh_headline(
    ticker: str,
    headline: str,
    summary: str | None = None,
    *,
    max_len: int = 56,
) -> str:
    """生成展示用中文标题；已是中文风格则原样返回。"""
    tick = (ticker or "").strip().upper() or "—"
    if not needs_zh_headline(headline):
        return (headline or "").strip()[:500]

    blob = f"{headline or ''}\n{summary or ''}"
    brief = _brief_from_text(blob, tick)
    if not brief:
        # 外文标题但摘要是英文：再试一次只用 summary
        brief = _brief_from_text(summary or "", tick)
    if not brief:
        clean = _SITE_SUFFIX.sub("", strip_html(headline or ""))[:40]
        brief = clean or "AI 相关通稿"

    brief = brief.strip(" ：:")
    if len(brief) > max_len - len(tick) - 1:
        brief = brief[: max_len - len(tick) - 2].rstrip() + "…"
    return f"{tick}：{brief}"


def rewrite_deal_headlines(
    db: Session,
    *,
    lookback_days: int | None = 365,
    limit: int = 200,
    dry_run: bool = False,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """批量把外文标题改成中文展示标题。"""
    from datetime import timedelta

    from app.deal_monitor.content_filter import should_hide_deal_event
    from app.database import DealEvent
    from app.source_url_guard import is_test_source_url
    from app.utils import now_beijing

    q = db.query(DealEvent)
    if lookback_days and lookback_days > 0:
        since = now_beijing() - timedelta(days=lookback_days)
        q = q.filter(DealEvent.published_at >= since)

    updated: list[dict] = []
    skipped = 0
    for event in q.order_by(DealEvent.published_at.desc()).limit(limit * 3).all():
        if is_test_source_url(event.source_url):
            skipped += 1
            continue
        if not include_hidden and should_hide_deal_event(event):
            skipped += 1
            continue
        if not needs_zh_headline(event.headline or ""):
            skipped += 1
            continue
        new_h = build_zh_headline(
            event.beneficiary_ticker or "",
            event.headline or "",
            event.summary,
        )
        if new_h == (event.headline or "").strip():
            skipped += 1
            continue
        info = {
            "id": event.id,
            "ticker": event.beneficiary_ticker,
            "old": (event.headline or "")[:80],
            "new": new_h,
        }
        if not dry_run:
            event.headline = new_h[:500]
        updated.append(info)
        if len(updated) >= limit:
            break

    if not dry_run and updated:
        try:
            db.commit()
        except Exception:
            db.rollback()
            for info in updated:
                ev = db.query(DealEvent).filter(DealEvent.id == info["id"]).first()
                if not ev:
                    continue
                ev.headline = info["new"][:500]
                try:
                    db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.warning("headline zh commit failed id=%s: %s", info["id"], exc)

    return {"updated": len(updated), "skipped": skipped, "samples": updated[:30]}
