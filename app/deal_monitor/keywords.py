"""中英关键词、负向词、合作触发词。"""

import re

POSITIVE_KEYWORDS = [
    # 中文
    "算力协议", "算力服务", "计算能力", "数据中心", "机房", "托管", "租赁协议",
    "容量协议", "GPU", "训练", "推理", "智算", "多年期", "独家", "千兆瓦", "兆瓦",
    # 英文
    "compute agreement", "capacity agreement", "cloud services agreement",
    "colocation", "hosting agreement", "data center lease", "AI infrastructure",
    "GPU deployment", "training capacity", "inference capacity",
    "multi-year", "gigawatt", "MW capacity", "power capacity",
    "hyperscaler", "lease", "definitive agreement", "material definitive agreement",
]

NEGATIVE_KEYWORDS = [
    "MOU", "memorandum of understanding", "explore partnership",
    "non-binding", "joint research", "academic collaboration",
    "谅解备忘录", "探索", "非约束",
]

COOPERATION_VERBS = [
    "sign", "signed", "signs", "enters", "enter into", "award", "awarded",
    "partner", "partnership", "collaborate", "agreement", "contract", "lease",
    "expand", "deploy", "announce", "announced", "合作", "签署", "协议",
]

UPDATE_KEYWORDS = ["amend", "expand", "extend", "追加", "上调"]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def find_matched_keywords(text: str) -> list[str]:
    norm = _normalize(text)
    matched = []
    for kw in POSITIVE_KEYWORDS:
        if kw.lower() in norm:
            matched.append(kw)
    return matched


def has_negative_dominance(text: str) -> bool:
    norm = _normalize(text)
    for kw in NEGATIVE_KEYWORDS:
        if kw.lower() in norm:
            return True
    if "strategic partnership" in norm:
        has_amount = bool(re.search(r"\$[\d,.]+\s*(million|billion|m\b|b\b)", norm, re.I))
        has_term = "multi-year" in norm or "year agreement" in norm or "多年" in norm
        if not has_amount and not has_term:
            return True
    return False


def has_cooperation_signal(text: str) -> bool:
    norm = _normalize(text)
    return any(v in norm for v in COOPERATION_VERBS)


def passes_keyword_filter(text: str) -> tuple[bool, list[str]]:
    matched = find_matched_keywords(text)
    if not matched:
        return False, []
    if has_negative_dominance(text):
        return False, matched
    if not has_cooperation_signal(text):
        return False, matched
    return True, matched


def is_update_headline(headline: str) -> bool:
    norm = _normalize(headline)
    return any(kw in norm for kw in UPDATE_KEYWORDS)
