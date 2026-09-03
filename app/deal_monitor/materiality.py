"""材料性打分 0～100（结合回测差距：软整合封顶、融资降权、电力合作加权）。"""

from __future__ import annotations

import re

from app.deal_monitor.keywords import NEGATIVE_KEYWORDS, _AI_SOFT_ESCAPE, is_product_only_integration

# 交易催化硬度
QUALITY_HARD = "hard"  # 金额/年限/容量/正式协议/hyperscaler 电力
QUALITY_SOFT_PRODUCT = "soft_product"  # 上架/发布/软整合
QUALITY_FINANCING = "financing"  # 融资/授信
QUALITY_MA = "m_and_a"  # 收购补强
QUALITY_NORMAL = "normal"

_HYPERSCALER = re.compile(
    r"\b(?:google|alphabet|microsoft|amazon|aws|azure|meta|nvidia|openai|anthropic)\b",
    re.I,
)
_POWER_STORAGE = re.compile(
    r"\b(?:power|energy|ppa|megawatt|gigawatt|\bmw\b|\bgw\b|storage|battery|"
    r"grid|electric|geothermal|data\s*center\s*power|电力|储能|供电)\b",
    re.I,
)
_FINANCING = re.compile(
    r"\b(?:financ(?:e|ing|ed)|raises?|raised|equity\s+raise|credit\s+facility|"
    r"debt\s+facility|underwrit|securitiz|bond\s+offering|follow[- ]on|"
    r"pipe\b|convertible\s+notes?|blue\s+owl)\b",
    re.I,
)
_MA = re.compile(
    r"\b(?:acquires?|acquired|acquisition|to\s+acquire|merger|buyout)\b",
    re.I,
)
_MARKETPLACE_LISTING = re.compile(
    r"\b(?:marketplace|app\s*store|available\s+in\s+the\s+\w+\s+marketplace|"
    r"brings?\s+the\s+\w+.{0,40}marketplace|listed\s+on)\b",
    re.I,
)
_SOFT_LAUNCH = re.compile(
    r"\b(?:unveils?|launches?|introduces?|rolls?\s+out|general\s+availability|"
    r"now\s+available|product\s+launch|agentic\s+system\s+for)\b",
    re.I,
)
_COMMERCIAL_TERMS = re.compile(
    r"(?:"
    r"\$[\d,.]+\s*(?:million|billion|m\b|b\b)|"
    r"multi-year|definitive\s+agreement|material\s+definitive|"
    r"capacity\s+agreement|supply\s+agreement|purchase\s+agreement|"
    r"\d+(\.\d+)?\s*(?:gw|gigawatt|mw|megawatt)|"
    r"entered\s+into|enter\s+into"
    r")",
    re.I,
)


def classify_deal_quality(text: str) -> str:
    norm = text or ""
    # 融资优先（即使带 $X billion，本质是募资不是客户签约）
    if _FINANCING.search(norm):
        if not (
            _HYPERSCALER.search(norm)
            and _POWER_STORAGE.search(norm)
            and re.search(r"\b(?:collaborate|partnership|ppa|power\s+purchase)\b", norm, re.I)
            and not re.search(r"\bfinanc(?:e|ing|ed)\b", norm, re.I)
        ):
            return QUALITY_FINANCING

    # 命名平台上架 hyperscaler 云：按硬催化（回测曾大涨）
    from app.deal_monitor.keywords import _PLATFORM_ON_CLOUD_RE

    if _PLATFORM_ON_CLOUD_RE.search(norm) and re.search(r"\bannounc\w+", norm, re.I):
        return QUALITY_HARD

    if _MA.search(norm) and not _COMMERCIAL_TERMS.search(norm):
        return QUALITY_MA
    if is_product_only_integration(norm) or (
        (_MARKETPLACE_LISTING.search(norm) or _SOFT_LAUNCH.search(norm))
        and not _COMMERCIAL_TERMS.search(norm)
        and not _PLATFORM_ON_CLOUD_RE.search(norm)
    ):
        return QUALITY_SOFT_PRODUCT
    if _HYPERSCALER.search(norm) and _POWER_STORAGE.search(norm) and re.search(
        r"\b(?:agreement|collaborat\w*|partnership|contract|provide|supply|deliver)\b",
        norm,
        re.I,
    ):
        return QUALITY_HARD
    # Claudeforce / Agentforce × 大模型：命名平台落地，按硬催化
    if re.search(r"\b(?:claudeforce|agentforce)\b", norm, re.I) and re.search(
        r"\b(?:anthropic|openai|claude)\b", norm, re.I
    ):
        return QUALITY_HARD
    if _COMMERCIAL_TERMS.search(norm):
        return QUALITY_HARD
    return QUALITY_NORMAL


def score_materiality(text: str, source: str, matched_keywords: list[str]) -> int:
    norm = text.lower()
    score = 40  # 基础分：已通过关键词+合作词筛选
    quality = classify_deal_quality(text)

    amount_match = re.search(
        r"\$[\d,.]+\s*(million|billion|m\b|b\b)|[\d,.]+\s*(million|billion)\s+dollars",
        norm,
        re.I,
    )
    if amount_match:
        val_str = amount_match.group(0)
        score += 25 if "billion" in val_str or re.search(r"\bb\b", val_str) else 15

    if re.search(r"\d+\s*-?\s*year|multi-year|多年", norm):
        score += 12

    if "definitive agreement" in norm or "material definitive agreement" in norm:
        score += 15
    elif "formal agreement" in norm or "正式协议" in norm:
        score += 10

    if re.search(r"\d+(\.\d+)?\s*(gw|gigawatt|mw|megawatt|兆瓦|千兆瓦)", norm):
        score += 15
    elif re.search(r"\d+\s*gpu", norm):
        score += 10

    # 企业 AI 平台：仅在有商业条款时大幅加分；纯上架/发布只给弱分
    soft_ai = any(tok in norm for tok in _AI_SOFT_ESCAPE)
    if soft_ai and re.search(
        r"integration|plugin|agentforce|claudeforce|copilot|product launch|announc",
        norm,
    ):
        if quality == QUALITY_SOFT_PRODUCT:
            score += 6
        else:
            score += 18

    # hyperscaler × 电力/储能/PPA：回测里常出现真涨（EOSE 等）
    if quality == QUALITY_HARD and _HYPERSCALER.search(norm) and _POWER_STORAGE.search(norm):
        score += 20

    if source in ("pr_newswire", "globe", "sec_8k") or source.startswith(
        ("finnhub:", "google_news", "business_wire", "ir:")
    ):
        score += 8

    coop_verbs = ["signs", "signed", "enters", "awards", "awarded", "lease", "collaboration"]
    if any(v in norm for v in coop_verbs):
        score += 5

    if len(matched_keywords) >= 3:
        score += 5

    for kw in NEGATIVE_KEYWORDS:
        if kw.lower() in norm:
            if "mou" in kw.lower() or "memorandum" in kw.lower() or "谅解" in kw:
                score -= 20
            elif "non-binding" in kw.lower() or "非约束" in kw:
                score -= 25
            elif "research" in kw.lower() or "academic" in kw.lower():
                score -= 30
            else:
                score -= 15

    if "strategic partnership" in norm and not amount_match and not soft_ai:
        score -= 15

    if quality == QUALITY_FINANCING:
        score -= 20
    elif quality == QUALITY_MA:
        score -= 12
    elif quality == QUALITY_SOFT_PRODUCT:
        score -= 15

    return max(0, min(100, score))


def finalize_materiality_score(
    text: str,
    source: str,
    matched_keywords: list[str],
    *,
    llm_score: int | None = None,
    event_type: str | None = None,
) -> int:
    """规则分 + LLM 分合并；按催化硬度封顶，避免软整合被打到 80+。"""
    base = score_materiality(text, source, matched_keywords)
    quality = classify_deal_quality(text)
    score = base
    et = (event_type or "").strip()

    if llm_score:
        if quality == QUALITY_SOFT_PRODUCT:
            # 产品上架/软整合：LLM 再高也压到弱催化区
            score = min(base + 5, int(llm_score), 58)
        elif quality == QUALITY_FINANCING:
            score = min(max(base, min(int(llm_score), base + 5)), 60)
        elif quality == QUALITY_MA:
            score = min(max(base, min(int(llm_score), base + 8)), 68)
        elif et == "ai_platform_deal":
            score = min(100, max(base, min(int(llm_score), base + 10)))
            if quality != QUALITY_HARD and not _COMMERCIAL_TERMS.search(text or ""):
                score = min(score, 62)
        else:
            score = min(100, max(base, min(int(llm_score), base + 10)))

    if quality == QUALITY_HARD and _HYPERSCALER.search(text or "") and _POWER_STORAGE.search(
        text or ""
    ):
        score = max(score, 78)

    # 命名平台上架 hyperscaler 云
    from app.deal_monitor.keywords import _PLATFORM_ON_CLOUD_RE

    if _PLATFORM_ON_CLOUD_RE.search(text or "") and re.search(r"\bannounc\w+", text or "", re.I):
        score = max(score, 75)

    # 运营商/电信 × Google Cloud / Azure / AWS 战略合作
    if re.search(r"\b(?:verizon|at&t|t-mobile|deutsche\s+telekom)\b", text or "", re.I) and re.search(
        r"\b(?:google cloud|microsoft azure|amazon web services|\baws\b)\b",
        text or "",
        re.I,
    ):
        score = max(score, 72)

    # Silicom 类 design win：小金额导入，限制上限
    if re.search(r"\bdesign\s+win\b", text or "", re.I) and not re.search(
        r"\$[\d,.]+\s*billion|\bgigawatt\b|multi-year", text or "", re.I
    ):
        score = min(score, 58)

    if quality == QUALITY_SOFT_PRODUCT:
        score = min(score, 58)
    elif quality == QUALITY_FINANCING:
        score = min(score, 60)

    return max(0, min(100, int(score)))
