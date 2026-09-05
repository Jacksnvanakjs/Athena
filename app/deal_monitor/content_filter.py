"""合作快讯内容质量过滤：只放行可交易的一手通稿。

产品目标：尽早抓住新签/新公告，第一时间买入赌涨跌。
股价反应稿、周报评论、SEO 洗稿、聚合源复述会丧失时效，必须在入库前拒绝。
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from app.deal_monitor.fetchers.pr_wire import RawItem

# 已知 SEO/洗稿/二手情绪站（source 名或域名，小写）
_DEFAULT_BLOCKED_SOURCES = frozenset(
    {
        "mshale",
        "mshale.com",
        "biztoc",
        "newsbreak",
        "smartnews",
        "flipboard",
        "stocktwits",
        "dailyhunt",
        "the deep dive",
        "deepdive",
        "seeking alpha",
        "motley fool",
        "tipranks",
        "investorplace",
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

# 标题以股价涨跌为主：说明已错过原稿，一律不入库/不展示
_PRICE_MOVE_HEADLINE = re.compile(
    r"(?:"
    r"(?:shares?|stock|equity)\s+(?:up|down|jumps?|jumped|surges?|surged|soars?|soared|"
    r"rally|rallies|rallied|rises?|rose|gains?|climbs?|climbed|adds?|leaps?|plunges?|tumbles?)"
    r"|(?:jumps?|jumped|surges?|surged|soars?|soared|rallies|rallied|rises?|rose|gains?|"
    r"climbs?|climbed|leaps?|plunges?)\s+(?:about\s+|over\s+|nearly\s+|more than\s+)?"
    r"\d{1,3}(?:\.\d+)?\s*%"
    r"|\b\d{1,3}(?:\.\d+)?\s*%\s+(?:gain|gains|rise|rises|jump|jumps|surge|surges|rally|rallies)"
    r"|大涨|暴涨|飙升|涨超|涨近|涨逾|涨约|大跌|暴跌"
    r")",
    re.I,
)

_PRICE_REACTION = re.compile(
    r"(?:"
    r"(?:shares?|stock|equity)\s+(?:up|jumps?|surges?|soars?|rally|rises?|gains?|climbs?|adds?|leaps?)"
    r"|(?:up|jumps?|surges?|soars?|rally|rises?|gains?|climbs?|leaps?)\s+\d{1,3}(?:\.\d+)?\s*%"
    r")"
    r".{0,100}?"
    r"(?:after|following|as|on)\s+",
    re.I,
)
_PRICE_REACTION_REV = re.compile(
    r"(?:after|following)\s+.{0,60}?(?:announc\w*|partnership|deal|agreement|collaborat\w*|launch|signs?)"
    r".{0,100}?"
    r"(?:shares?|stock|up\s+\d{1,3}(?:\.\d+)?\s*%|jumps?|surges?|soars?|rally)",
    re.I,
)

# 评论/展望/复盘：对盘前抢跑几乎无用
_COMMENTARY_HEADLINE = re.compile(
    r"(?:"
    r"\bcould reshape\b|\bkeeps rewriting\b|\bwhat(?:'s| is) next\b|"
    r"\bis (?:a |an )?.{0,50}?\benough\b|"
    r"\binvestors?\b.{0,40}?\b(?:should|may|might|need to)\b|"
    r"\bwhy\b.{0,50}?\b(?:stock|shares|investors?)\b|"
    r"\bhow\b.{0,80}?\bcould\b|"
    r"\bthis week in\b|\bweekly roundup\b|\bweekly recap\b|\bmarket wrap\b|"
    r"\bweek in (?:review|optics|ai|tech)\b|"
    r"\bopinion\b|\banalysis:\b|\bthe deep dive\b|"
    r"\brewrites? (?:its|their) own forecast\b|"
    r"\bpower surge could\b|"
    # 中文解读标题常见二手标记
        r"同上周报|同主题|周报|行业复盘|本周回顾|解读稿|偏事后"
    r")",
    re.I,
)

# 一手通道：公司 IR / 新闻稿 / SEC，不做「聚合源须像新签」约束
_PRIMARY_SOURCE_PREFIXES = (
    "ir:",
    "sec_8k",
    "pr_newswire",
    "business_wire",
    "globenewswire",
    "company_ir",
)

# 弱催化：运营琐事、无新商业条款、挖矿转型叙事
_WEAK_CATALYST = re.compile(
    r"(?:"
    r"ceased?\s+(?:bitcoin\s+)?mining|"
    r"stop(?:s|ped)?\s+(?:bitcoin\s+)?mining|"
    r"exit(?:s|ing)?\s+(?:bitcoin\s+)?mining|"
    r"has ceased\b|"
    r"停挖矿|停止挖矿|挖矿转|"
    r"opens?\s+(?:a\s+)?new\s+.{0,30}office|"
    r"receives?\s+accreditation|"
    r"achieves?\s+.{0,40}accreditation|"
    r"\brebuilds?\s+its\b|"
    r"\bmaking a decade of\b|"
    r"\bapproved;?\s+latest approval\b|"
    r"\bportfolio of large-load contracts\b"
    r")",
    re.I,
)

_NAMED_AI_COUNTERPARTY = re.compile(
    r"\b(?:openai|anthropic|google|microsoft|amazon|nvidia|meta|oracle|aws|azure|"
    r"claude|chatgpt|copilot|agentforce)\b",
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
    if _PRICE_MOVE_HEADLINE.search(norm):
        return True
    if _PRICE_REACTION.search(norm) or _PRICE_REACTION_REV.search(norm):
        return True
    if re.search(
        r"\b\d{1,3}(?:\.\d+)?\s*%\s+(?:gain|rise|jump|surge|rally)\b.{0,60}?"
        r"(?:after|following|as)\s+",
        norm,
    ):
        return True
    return False


def is_market_commentary(text: str) -> bool:
    """评论/周报/行业复盘，不是可交易的一阶通稿。"""
    return bool(_COMMENTARY_HEADLINE.search(_norm(text)))


def is_weak_price_catalyst(text: str) -> bool:
    """运营琐事/弱叙事：对推动股价帮助很小。"""
    norm = _norm(text)
    if not _WEAK_CATALYST.search(norm):
        return False
    # 若同时有明确大模型/云厂对手方 + 协议措辞，保留
    if _NAMED_AI_COUNTERPARTY.search(norm) and (
        is_material_signed_deal(text)
        or re.search(r"\b(?:partnership|agreement|contract|collaboration)\b", norm)
    ):
        return False
    return True


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
    if norm.startswith("brief-") or norm.startswith("brief -"):
        # BRIEF 二次转载，优先等一手源
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
        "enters into",
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
        "power purchase agreement",
        "power purchase",
        "signed a ppa",
        "signs ppa",
        "sign a ppa",
        "multi-year agreement",
        "strategic partnership to",
        "expand partnership",
        "expands partnership",
        "expanded partnership",
        "deepens partnership",
        "deepen partnership",
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


def _is_primary_wire_source(source: str) -> bool:
    src = (source or "").lower().strip()
    return any(src == p or src.startswith(p) for p in _PRIMARY_SOURCE_PREFIXES)


def reject_deal_item(item: RawItem) -> tuple[bool, str]:
    """
    入库第一步：非一手/已失去时效 → 拒绝。
    仅 IR / PR 通稿 / SEC 视为一手通道；Google News / Finnhub 即使路透也须像新签通告。
    """
    headline = item.headline or ""
    text = f"{headline}\n{item.summary or ''}"
    src = (item.source or "").lower()

    if is_blocked_news_source(item):
        return True, "低信源/SEO 站点"

    if is_seo_spam_headline(headline):
        return True, "标题 SEO 灌水"

    # 标题已写股价大涨/大跌：市场已反应完，无抢跑价值
    if is_price_reaction_rehash(headline):
        return True, "旧闻复述（股价反应稿）"

    if is_market_commentary(headline) or is_market_commentary(text):
        return True, "评论/展望稿，非一阶通稿"

    if is_weak_price_catalyst(text):
        return True, "弱催化/运营琐事"

    # 聚合/转载源：必须像「刚宣布的新签」，白名单媒体也不豁免
    if not _is_primary_wire_source(src) and (
        src.startswith("google_news:") or src.startswith("finnhub:") or src.startswith("google_news")
    ):
        if not is_fresh_deal_announcement(text):
            norm = _norm(text)
            strong = any(
                p in norm
                for p in (
                    "definitive agreement",
                    "material definitive",
                    "entered into",
                    "enters into",
                    "enter into",
                    "item 1.01",
                    "multi-year",
                    "today announced",
                    "announced today",
                )
            )
            if not strong and not is_material_signed_deal(text):
                return True, "聚合源且非新签通稿"

    return False, ""


def should_hide_deal_content(
    headline: str,
    summary: str | None,
    source: str | None,
    source_url: str | None,
    published_at=None,
) -> bool:
    """列表/API：与入库同一标准，应拒则隐藏（不删库）。"""
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
    """展示与入库一致：第一步会拒的，列表一律不展示（不因首日涨跌豁免）。"""
    from datetime import datetime, timezone

    item = RawItem(
        headline=event.headline or "",
        summary=event.summary or "",
        source=event.source or "",
        source_url=event.source_url or "",
        published_at=getattr(event, "published_at", None) or datetime.now(timezone.utc),
    )
    if reject_deal_item(item)[0]:
        return True
    # 标题已标明挖矿转型等弱叙事：即使正文有大额 MSA 也不展示
    headline = event.headline or ""
    if is_weak_price_catalyst(headline):
        return True
    return False


def should_hide_weak_quality_event(event, *, keep_if_first_day_high: bool = True) -> bool:
    """列表可选：隐藏软整合/融资/空话；首日回测≥70 的保留作对照样本。"""
    from app.deal_monitor.materiality import (
        classify_deal_quality,
        is_weak_quality_for_display,
    )

    text = f"{event.headline or ''}\n{event.summary or ''}"
    quality = classify_deal_quality(text)
    if not is_weak_quality_for_display(quality):
        return False
    fd = getattr(event, "first_day_score", None)
    if keep_if_first_day_high and fd is not None and int(fd) >= 70:
        return False
    return True



def deal_amount_keys(text: str) -> set[str]:
    """提取金额指纹，用于同故事去重。"""
    norm = _norm(text)
    keys: set[str] = set()
    for m in re.finditer(
        r"\$?\s*([\d,.]+)\s*(billion|million|bn|b\b|m\b)",
        norm,
    ):
        num = m.group(1).replace(",", "")
        unit = m.group(2)
        if unit.startswith("b"):
            keys.add(f"{num}b")
        else:
            keys.add(f"{num}m")
    for m in re.finditer(r"\$?\s*([\d,.]+)\s*亿", text):
        keys.add(f"{m.group(1).replace(',', '')}yi")
    return keys
