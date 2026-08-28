"""正文清洗：去 HTML / URL，避免实体误匹配。"""

from __future__ import annotations

import re

_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)


def strip_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def strip_urls(text: str) -> str:
    text = _URL_RE.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def clean_article_text(text: str) -> str:
    return strip_urls(strip_html(text))
