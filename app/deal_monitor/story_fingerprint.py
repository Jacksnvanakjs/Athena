"""新闻内容指纹：用正文摘要为主、标题为辅，识别是否同一篇故事。

仅靠标题哈希不够：通稿转载常改标题；不同事件也可能撞相似标题。
"""

from __future__ import annotations

import hashlib
import re

from app.text_clean import clean_article_text

_WS = re.compile(r"\s+")
_NOISE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_story_text(headline: str | None, summary: str | None) -> str:
    """清洗后的可比正文；摘要够长时以摘要为主。"""
    body = clean_article_text(summary or "")
    head = clean_article_text(headline or "")
    if len(body) >= 60:
        blob = body
    else:
        blob = f"{head}\n{body}".strip()
    blob = _NOISE.sub(" ", blob.lower())
    return _WS.sub(" ", blob).strip()[:1200]


def story_fingerprint(headline: str | None, summary: str | None) -> str:
    """内容指纹（MD5）。空文返回空串。"""
    blob = normalize_story_text(headline, summary)
    if not blob:
        return ""
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def is_same_story(
    headline_a: str | None,
    summary_a: str | None,
    headline_b: str | None,
    summary_b: str | None,
    *,
    url_a: str | None = None,
    url_b: str | None = None,
) -> bool:
    """同一 URL，或正文指纹相同，或长摘要互为前缀（截断差异）→ 视为同文。"""
    ua = (url_a or "").strip()
    ub = (url_b or "").strip()
    if ua and ub and ua == ub:
        return True

    fa = story_fingerprint(headline_a, summary_a)
    fb = story_fingerprint(headline_b, summary_b)
    if fa and fb and fa == fb:
        return True

    na = normalize_story_text(headline_a, summary_a)
    nb = normalize_story_text(headline_b, summary_b)
    if len(na) >= 80 and len(nb) >= 80:
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        # 同源摘要长短不一：取较短侧前 400 字作前缀匹配
        tip = shorter[: min(len(shorter), 400)]
        if tip and longer.startswith(tip):
            return True
    return False


def is_related_deal_story(
    headline_a: str | None,
    summary_a: str | None,
    headline_b: str | None,
    summary_b: str | None,
    *,
    url_a: str | None = None,
    url_b: str | None = None,
    same_anchor: bool = False,
) -> bool:
    """同文，或（同锚点时）短转载与长通稿共享合作线索。

    Finnhub 等常把旧 PPA 改写成一两句「building on partnership…」，
    指纹对不上，但受益方+锚点+购电/算力线索仍应视为同一故事。
    """
    if is_same_story(
        headline_a,
        summary_a,
        headline_b,
        summary_b,
        url_a=url_a,
        url_b=url_b,
    ):
        return True
    if not same_anchor:
        return False

    from app.deal_monitor.content_filter import deal_amount_keys, shared_deal_story_cues

    blob_a = f"{headline_a or ''}\n{summary_a or ''}"
    blob_b = f"{headline_b or ''}\n{summary_b or ''}"
    if deal_amount_keys(blob_a) & deal_amount_keys(blob_b):
        return True
    cues = shared_deal_story_cues(blob_a, blob_b)
    if not cues:
        return False
    # 至少一类合作线索重合，且两边都像在说「协议/合作」而非无关点评
    dealish = re.compile(
        r"\b(?:agreement|deal|contract|partnership|ppa|lease|customer|signed|signs)\b|"
        r"协议|合作|签署|购电|签约",
        re.I,
    )
    return bool(dealish.search(blob_a) and dealish.search(blob_b))
