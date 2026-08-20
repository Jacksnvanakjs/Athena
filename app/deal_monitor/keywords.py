"""中英关键词、负向词、合作触发词。"""

import re

POSITIVE_KEYWORDS = [
    # 中文
    "算力协议", "算力服务", "计算能力", "数据中心", "机房", "托管", "租赁协议",
    "容量协议", "GPU", "训练", "推理", "智算", "多年期", "独家", "千兆瓦", "兆瓦",
    # 英文（领域 + 合作语义混合）
    # 英文
    "compute agreement", "capacity agreement", "cloud services agreement",
    "colocation", "hosting agreement", "data center lease", "AI infrastructure",
    "GPU deployment", "training capacity", "inference capacity",
    "multi-year", "gigawatt", "MW capacity", "power capacity",
    "hyperscaler", "definitive agreement", "material definitive agreement",
    # 为了不让命中率为 0：加入少量“基础领域词”
    "lease", "data center", "data centre", "infrastructure", "cloud", "AI", "artificial intelligence",
]

# 有效性兜底：必须至少包含一个明显的“算力/数据中心/GPU/容量”领域信号
HIGH_VALUE_TOKENS = [
    "ai",
    "artificial intelligence",
    "人工智能",
    "gpu",
    "compute",
    "data center",
    "data centre",
    "ai infrastructure",
    "colocation",
    "capacity",
    "power capacity",
    "megawatt",
    "gigawatt",
    "托管",
    "算力",
    "数据中心",
    "机房",
    "智算",
    "容量",
    "租赁协议",
    "训练",
    "推理",
    # 定制硅 / 互联 / 先进封装（避免只认 GPU/机柜）
    "custom semiconductor",
    "custom silicon",
    "asic",
    "tpu",
    "inference accelerator",
    "hbm",
    "hyperscale",
    "advanced packaging",
    "infiniband",
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


def passes_keyword_filter(text: str, source: str = "") -> tuple[bool, list[str]]:
    del source  # 8-K 也走同一套 AI/算力领域词，不能只凭 Item 1.01 入库
    matched = find_matched_keywords(text)
    if not matched:
        return False, []
    if has_negative_dominance(text):
        return False, matched
    if not has_cooperation_signal(text):
        return False, matched
    # 兜底：避免泛泛 lease/cloud 触发
    norm = _normalize(text)
    if not any(tok.lower() in norm for tok in HIGH_VALUE_TOKENS):
        return False, matched
    return True, matched


def is_update_headline(headline: str) -> bool:
    norm = _normalize(headline)
    return any(kw in norm for kw in UPDATE_KEYWORDS)
