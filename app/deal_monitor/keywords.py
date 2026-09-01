"""中英关键词、负向词、合作触发词。"""

import re

POSITIVE_KEYWORDS = [
    # 中文
    "算力协议", "算力服务", "计算能力", "数据中心", "机房", "托管", "租赁协议",
    "容量协议", "GPU", "训练", "推理", "智算", "多年期", "独家", "千兆瓦", "兆瓦",
    "战略合作", "产品整合", "人工智能",
    # 英文（领域 + 合作语义混合）
    "compute agreement", "capacity agreement", "cloud services agreement",
    "colocation", "hosting agreement", "data center lease", "AI infrastructure",
    "GPU deployment", "training capacity", "inference capacity",
    "multi-year", "gigawatt", "MW capacity", "power capacity",
    "hyperscaler", "definitive agreement", "material definitive agreement",
    # 为了不让命中率为 0：加入少量“基础领域词”
    "lease", "data center", "data centre", "infrastructure", "cloud", "AI", "artificial intelligence",
    # 企业 AI 平台 / Agent
    "Agentforce", "Claudeforce", "AI agent", "agentic", "Anthropic", "OpenAI",
    "Claude", "Copilot", "product integration", "strategic partnership",
]

# 有效性兜底：必须至少包含一个明显的“算力/数据中心/GPU/容量/企业AI”领域信号
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
    # 企业 AI 平台
    "anthropic",
    "openai",
    "claude",
    "agentforce",
    "claudeforce",
    "ai agent",
    "agentic",
    "llm",
    "generative ai",
    "copilot",
]

NEGATIVE_KEYWORDS = [
    "MOU", "memorandum of understanding", "explore partnership",
    "non-binding", "joint research", "academic collaboration",
    "谅解备忘录", "探索", "非约束",
]

# strategic partnership 若伴随企业 AI/大模型产品信号，不当作负向
_AI_SOFT_ESCAPE = (
    "anthropic", "openai", "claude", "ai agent", "agentic", "agentforce",
    "claudeforce", "llm", "generative ai", "copilot", "large language model",
)

COOPERATION_VERBS = [
    "sign", "signed", "signs", "enters", "enter into", "award", "awarded",
    "partner", "partnership", "collaborate", "agreement", "contract", "lease",
    "expand", "deploy", "announce", "announced", "合作", "签署", "协议",
]

UPDATE_KEYWORDS = ["amend", "extend", "追加", "上调"]

_PRODUCT_ONLY_CUES = (
    "deepens integration",
    "product integration",
    "integrates with",
    "integration enables",
    "launches new",
    "unveils",
    "introduces",
    "now available",
    "general availability",
)
_STRONG_DEAL_CUES = (
    "definitive agreement",
    "material definitive",
    "entered into",
    "enter into",
    "commercial agreement",
    "multi-year",
    "capacity agreement",
    "purchase agreement",
    "supply agreement",
    "item 1.01",
    "today announced",
    "announced that",
    "announced the",
    "announces partnership",
    "deepens partnership",
)
_HYPERSCALER_CLOUD_CUES = (
    "google cloud",
    "amazon web services",
    "microsoft azure",
    " aws ",
    " azure ",
)
_PLATFORM_ON_CLOUD_RE = re.compile(
    r"\b(?:now )?available on\b.{0,40}?\b(?:google cloud|aws|amazon web services|microsoft azure|azure)\b",
    re.I,
)


def is_product_only_integration(text: str) -> bool:
    """纯产品整合/功能发布，无新商业条款。"""
    norm = _normalize(text)
    if _PLATFORM_ON_CLOUD_RE.search(norm) and re.search(r"\bannounc\w+", norm):
        return False
    if not any(cue in norm for cue in _PRODUCT_ONLY_CUES):
        return False
    if any(cue in norm for cue in _STRONG_DEAL_CUES):
        return False
    if any(cloud in norm for cloud in _HYPERSCALER_CLOUD_CUES) and re.search(
        r"\b(?:platform|falcon|security)\b", norm
    ) and re.search(r"\bannounc\w+", norm):
        return False
    if re.search(r"\$[\d,.]+\s*(million|billion|m\b|b\b)", norm, re.I):
        return False
    return True


def is_update_headline(headline: str) -> bool:
    """
    仅对「修订/延期/上调」类标题放行 7 天去重豁免。
    「expand partnership/collaboration」是新合作通稿常见措辞，不算 update。
    """
    norm = _normalize(headline)
    if "expand partnership" in norm or "expand collaboration" in norm:
        return False
    if "announc" in norm or "launch" in norm or " unveil" in norm:
        return False
    if "amend" in norm:
        return True
    if "extend" in norm and any(w in norm for w in ("agreement", "term", "capacity", "lease")):
        return True
    if "追加" in norm or "上调" in norm:
        return True
    return False


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
        # 企业 AI / 大模型产品合作：不过度误杀
        if any(tok in norm for tok in _AI_SOFT_ESCAPE):
            return False
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

