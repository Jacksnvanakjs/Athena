"""合作快讯内容质量过滤：SEO 垃圾站、旧闻复述、标题灌水。"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from app.deal_monitor.fetchers.pr_wire import RawItem

# 已知 SEO/洗稿站（source 名或域名，小写）
_DEFAULT_BLOCKED_SOURCES = frozenset(
    {
        "mshale",
        "mshale.com",
        "biztoc",
        "newsbreak",
        "smartnews",
        "flipboard",
    }
)

_EXTRA_BLOCKED = {
    s.strip().lower()
    for s in os.getenv("DEAL_BLOCKED_NEWS_SOURCES", "").split(",")
    if s.strip()
}
BLOCKED_NEWS_SOURCES = _DEFAULT_BLOCKED_SOURCES | _EXTRA_BLOCKED

# Google News / 转载源名白名单（路透、WSJ 等；小写匹配 source 冒号后名称）
_TRUSTED_NEWS_TOKENS = frozenset(
    {
        "reuters",
        "wsj",
        "wall street journal",
        "bloomberg",
        "bloomberg.com",
        "cnbc",
        "financial times",
        "ft.com",
        "the information",
        "yahoo finance",
        "ap news",
        "associated press",
        "business wire",
        "pr newswire",
        "globenewswire",
        "channel news asia",
        "benzinga",
        "marketwatch",
        "barron",
        "investing.com",
        "afp",
    }
)

# sign(s/ed) + 金额 + deal/agreement（中间可夹修饰语）
_MATERIAL_SIGNED_DEAL = re.compile(
    r"\b(?:signs?|signed|signing)\b.{0,80}?"
    r"(?:\$[\d,.]+\s*(?:billion|million|bn|m\b)|[\d,.]+\s*(?:billion|million)\s+dollars?)"
    r".{0,60}?\b(?:deal|agreement|contract|lease)\b",
    re.I,
)
_MATERIAL_SIGNED_DEAL_REV = re.compile(
    r"\b(?:deal|agreement|contract|lease)\b.{0,60}?"
    r"(?:worth|valued at|for)\s+"
    r"(?:\$[\d,.]+\s*(?:billion|million|bn|m\b)|[\d,.]+\s*(?:billion|million)\s+dollars?)",
    re.I,
)
_SIGN_DEAL_FLEX = re.compile(
    r"\b(?:signs?|signed|signing|has signed|have signed)\b.{0,120}?\b(?:deal|agreement|contract)\b",
    re.I,
)
_PRICE_REACTION = re.compile(
    r"(?:"
    r"(?:shares?|stock|equity)\s+(?:up|jumps?|surges?|soars?|rally|rises?|gains?|climbs?|adds?|leaps?)"
    r"|(?:up|jumps?|surges?|soars?|rally|rises?|gains?|climbs?|leaps?)\s+\d{1,3}\s*%"
    r")"
    r".{0,80}?"
    r"(?:after|following)\s+.{0,40}?(?:announc\w*|partnership|deal|agreement|collaborat\w*)",
    re.I,
)
_PRICE_REACTION_REV = re.compile(
    r"(?:after|following)\s+.{0,40}?(?:announc\w*|partnership|deal|agreement|collaborat\w*)"
    r".{0,100}?"
    r"(?:shares?|stock|up\s+\d{1,3}\s*%|jumps?|surges?|soars?|rally)",
    re.I,
)

# 标题末尾随机串，常见于 SEO 农场
_SEO_HASH_TOKEN = re.compile(r"\([A-Za-z0-9]{6,14}\)")

# 娱乐/剧集关键词与财经关键词混排
_ENTERTAINMENT_CUES = (
    "below deck",
    "mediterranean season",
    "season 11",
    "season 10",
    "episode",
    "watch online",
    "streaming",
    "reality tv",
    "full episode",
    "cast members",
)
_FINANCE_CUES = (
    "shares",
    "stock",
    "partnership",
    "openai",
    "nvda",
    "earnings",
    "announc",
    "agreement",
    "deal",
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _source_tokens(item: RawItem) -> set[str]:
    out: set[str] = set()
    src = (item.source or "").strip().lower()
    if src:
        out.add(src)
        if ":" in src:
            out.add(src.split(":", 1)[1].strip())
    url = (item.source_url or "").strip()
    if url:
        try:
            host = (urlparse(url).hostname or "").lower()
            if host:
                out.add(host)
                out.add(host.removeprefix("www."))
        except Exception:
            pass
    return {t for t in out if t}


def is_blocked_news_source(item: RawItem) -> bool:
    tokens = _source_tokens(item)
    for tok in tokens:
        if tok in BLOCKED_NEWS_SOURCES:
            return True
        for blocked in BLOCKED_NEWS_SOURCES:
            if tok.endswith(blocked) or blocked in tok:
                return True
    return False


def is_trusted_news_source(item: RawItem) -> bool:
    tokens = _source_tokens(item)
    for tok in tokens:
        for trusted in _TRUSTED_NEWS_TOKENS:
            if tok == trusted or trusted in tok or tok in trusted:
                return True
    return False


def is_material_signed_deal(text: str) -> bool:
    """大额签署/签约报道（含路透体 sign $35 billion ... deal）。"""
    norm = _norm(text)
    if _MATERIAL_SIGNED_DEAL.search(norm) or _MATERIAL_SIGNED_DEAL_REV.search(norm):
        return True
    if _SIGN_DEAL_FLEX.search(norm) and re.search(
        r"\$[\d,.]+\s*(?:billion|million|bn)\b|[\d,.]+\s*billion",
        norm,
    ):
        return True
    if re.search(r"\d+\s*亿", text) and re.search(r"签署|签约|协议", text):
        return True
    return False


def is_price_reaction_rehash(text: str) -> bool:
    """旧闻复述：强调股价涨跌而非新签署/新条款。"""
    norm = _norm(text)
    if _PRICE_REACTION.search(norm) or _PRICE_REACTION_REV.search(norm):
        return True
    # 「涨 X% 后宣布/合作」类中文少见，英文为主
    if re.search(
        r"\b\d{1,3}\s*%\s+(?:gain|rise|jump|surge|rally)\b.{0,60}?"
        r"(?:after|following)\s+(?:announc|partnership|deal)",
        norm,
    ):
        return True
    return False


def is_seo_spam_headline(headline: str) -> bool:
    h = headline or ""
    norm = _norm(h)
    if _SEO_HASH_TOKEN.search(h):
        return True
    has_fin = any(c in norm for c in _FINANCE_CUES)
    has_ent = any(c in norm for c in _ENTERTAINMENT_CUES)
    if has_fin and has_ent:
        return True
    # 标题过长且含多个破折号分隔的无关片段
    if len(h) > 120 and h.count(" - ") >= 2:
        return True
    return False


def is_fresh_deal_announcement(text: str) -> bool:
    """新合作通稿常见措辞；纯股价反应稿通常没有。"""
    if is_material_signed_deal(text):
        return True
    norm = _norm(text)
    if _SIGN_DEAL_FLEX.search(norm):
        return True
    fresh = (
        "enter into",
        "entered into",
        "signs agreement",
        "signed agreement",
        "signs deal",
        "signed deal",
        "definitive agreement",
        "material definitive",
        "commercial agreement",
        "capacity agreement",
        "supply agreement",
        "multi-year agreement",
        "strategic partnership to",
        "announced today",
        "today announced",
        "announced that it",
        "announced the",
        "announced a partnership",
        "announced an agreement",
        "announces partnership",
        "announces agreement",
        "announces a multi",
        "签署",
        "正式协议",
        "达成.*协议",
    )
    if any(re.search(p, norm) for p in fresh if ".*" in p):
        return True
    return any(p in norm for p in fresh if ".*" not in p)


def reject_deal_item(item: RawItem) -> tuple[bool, str]:
    """
    返回 (应拒绝, 原因)。
    SEC / 公司 IR / PR Newswire 不受 Google 低信源规则约束。
    """
    headline = item.headline or ""
    text = f"{headline}\n{item.summary or ''}"
    src = (item.source or "").lower()

    if is_blocked_news_source(item):
        return True, "低信源/SEO 站点"

    if is_seo_spam_headline(headline):
        return True, "标题 SEO 灌水"

    if is_price_reaction_rehash(text):
        # 股价反应稿：除非同时像「今日新签」通稿，否则视为旧闻复述
        if not is_fresh_deal_announcement(text):
            return True, "旧闻复述（股价反应稿）"

    trusted = is_trusted_news_source(item)
    # Google News 非权威源：需更像新签通稿；白名单媒体（路透/WSJ 等）豁免
    if src.startswith("google_news:") and not src.startswith("google_news:pr ") and not trusted:
        if is_price_reaction_rehash(headline) or not is_fresh_deal_announcement(text):
            norm = _norm(text)
            strong = any(
                p in norm
                for p in (
                    "definitive agreement",
                    "material definitive",
                    "entered into",
                    "enter into",
                    "item 1.01",
                    "multi-year",
                )
            )
            if not strong and not is_material_signed_deal(text):
                return True, "Google News 低信源且非新签通稿"

    return False, ""


def should_hide_deal_content(
    headline: str,
    summary: str | None,
    source: str | None,
    source_url: str | None,
    published_at=None,
) -> bool:
    """列表/API 展示时隐藏应被拒的低信源、旧闻复述稿（不删库）。"""
    from datetime import datetime, timezone

    item = RawItem(
        headline=headline or "",
        summary=summary or "",
        source=source or "",
        source_url=source_url or "",
        published_at=published_at or datetime.now(timezone.utc),
    )
    return reject_deal_item(item)[0]


def should_hide_deal_event(event) -> bool:
    return should_hide_deal_content(
        event.headline,
        event.summary,
        event.source,
        event.source_url,
        event.published_at,
    )
