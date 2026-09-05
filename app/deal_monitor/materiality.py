"""材料性打分 0～100（对齐首日回测：虚高合作压分、硬条款抬分）。"""

from __future__ import annotations

import re

from app.deal_monitor.keywords import NEGATIVE_KEYWORDS, _AI_SOFT_ESCAPE, is_product_only_integration

# 交易催化硬度
QUALITY_HARD = "hard"  # 金额/年限/容量/正式协议/hyperscaler 电力
QUALITY_SOFT_PRODUCT = "soft_product"  # 上架/发布/软整合
QUALITY_FINANCING = "financing"  # 融资/授信
QUALITY_MA = "m_and_a"  # 并购补强
QUALITY_VAGUE = "vague"  # 「战略合作」空话、无商业条款
QUALITY_NORMAL = "normal"

# 分数 vs 回测分（高85/中高70/中55/低35）差≥此值视为分差大（重打分）
SCORE_OUTCOME_GAP = 15
# 列表高亮可略严，避免满屏橙色
SCORE_OUTCOME_GAP_DISPLAY = 20

_HYPERSCALER = re.compile(
    r"\b(?:google|alphabet|microsoft|amazon|aws|azure|meta|nvidia|openai|anthropic)\b",
    re.I,
)
_POWER_STORAGE = re.compile(
    r"\b(?:power|energy|ppa|megawatt|gigawatt|\bmw\b|\bgw\b|storage|battery|"
    r"grid|electric|geothermal|data\s*center\s*power|电力|储能|供电|用电)\b",
    re.I,
)
# 真正的融资事件；避免命中 “financing capabilities” 等套话
_FINANCING = re.compile(
    r"(?:"
    r"\b(?:debt|equity|project|ai\s+factory|infrastructure)\s+financ(?:e|ing|ed)\b|"
    r"\bfinanc(?:e|ing|ed)\s+(?:for|of|round|package|facility|agreement)\b|"
    r"\b(?:raises?|raised)\s+\$|"
    r"\bcredit\s+facility\b|"
    r"\bdebt\s+facility\b|"
    r"\bunderwrit(?:e|es|ing|ten)\b|"
    r"\bsecuritiz|"
    r"\bbond\s+offering\b|"
    r"\bfollow[- ]on\s+(?:offering|equity)\b|"
    r"\bpipe\s+(?:investment|financing)\b|"
    r"\bconvertible\s+notes?\b|"
    r"\bblue\s+owl\b.{0,40}\bfinanc"
    r")",
    re.I,
)
_MA = re.compile(
    r"(?:"
    r"\b(?:acquires?|acquired|acquisition|to\s+acquire|merger|buyout)\b|"
    r"\bannounces?\s+sale\s+to\b|"
    r"\bsale\s+to\b.{0,60}\bfor\s+\$|"
    r"\bto\s+be\s+acquired\b"
    r")",
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
_GPU_DEPLOY_SOFT = re.compile(
    r"\b(?:completes?\s+deployment|deployment\s+of\s+(?:all\s+)?\d+\s+nvidia|"
    r"deploys?\s+\d+\s+nvidia|rtx\s+pro\s+\d+)\b",
    re.I,
)
_MINING_PIVOT = re.compile(
    r"(?:"
    r"ceased?\s+(?:bitcoin\s+)?mining|"
    r"stop(?:s|ped)?\s+(?:bitcoin\s+)?mining|"
    r"exit(?:s|ing)?\s+(?:bitcoin\s+)?mining|"
    r"停挖矿|停止挖矿|挖矿转|转\s*AI"
    r")",
    re.I,
)
_COMMERCIAL_TERMS = re.compile(
    r"(?:"
    r"\$[\d,.]+\s*(?:million|billion|m\b|b\b)|"
    r"multi-year|definitive\s+agreement|material\s+definitive|"
    r"capacity\s+agreement|supply\s+agreement|purchase\s+agreement|"
    r"master\s+services?\s+agreement|"
    r"\d+(\.\d+)?\s*(?:gw|gigawatt|mw|megawatt)|"
    r"\d+\s*-?\s*year.{0,50}(?:power|energy|ppa|用电|电力).{0,20}(?:deal|agreement|contract)|"
    r"sign(?:s|ed)?.{0,40}(?:power|energy)\s+deal|"
    r"\bpower\s+deal\b|"
    r"entered\s+into|enter\s+into"
    r")",
    re.I,
)
_VAGUE_COOP = re.compile(
    r"\b(?:strategic\s+partnership|strategic\s+collaboration|"
    r"collaborate(?:s|d)?\s+with|working\s+with|team(?:s|ed)?\s+up\s+with|"
    r"partners\s+with|partnership\s+with|explores?\s+collaboration|"
    r"mou\b|memorandum\s+of\s+understanding|"
    r"expanded?\s+collaboration|deepens?\s+collaboration|"
    r"扩大.{0,8}合作|加深.{0,8}协作)\b",
    re.I,
)
_NAMED_PLATFORM = re.compile(r"\b(?:claudeforce|agentforce)\b", re.I)
_TELCO = re.compile(
    r"\b(?:verizon|at&t|t-mobile|deutsche\s+telekom|vodafone|orange|telefonica)\b",
    re.I,
)
_CLOUD_HYPER = re.compile(
    r"\b(?:google cloud|microsoft azure|amazon web services|\baws\b|gcp\b)\b",
    re.I,
)
_FRONTIER_GPU = re.compile(
    r"\b(?:instinct|mi\d{2,3}|h100|h200|b200|gb200|rtx\s+pro|blackwell|hopper)\b",
    re.I,
)
_FRONTIER_LAB = re.compile(r"\b(?:anthropic|openai|google deepmind|deepmind)\b", re.I)


def has_commercial_terms(text: str) -> bool:
    return bool(_COMMERCIAL_TERMS.search(text or ""))


def is_telco_cloud_deal(text: str) -> bool:
    return bool(_TELCO.search(text or "") and _CLOUD_HYPER.search(text or ""))


def is_frontier_gpu_deploy(text: str) -> bool:
    """大模型实验室 + 具名 GPU/加速器部署（如 AMD Instinct × Anthropic）。"""
    return bool(_FRONTIER_LAB.search(text or "") and _FRONTIER_GPU.search(text or ""))


def classify_deal_quality(text: str) -> str:
    norm = text or ""
    if _MINING_PIVOT.search(norm):
        return QUALITY_SOFT_PRODUCT

    if _FINANCING.search(norm):
        # hyperscaler 电力合作正文偶尔带融资套话，不误杀
        if not (
            _HYPERSCALER.search(norm)
            and _POWER_STORAGE.search(norm)
            and re.search(r"\b(?:collaborate|partnership|ppa|power\s+purchase|agreement)\b", norm, re.I)
        ):
            return QUALITY_FINANCING

    from app.deal_monitor.keywords import _PLATFORM_ON_CLOUD_RE

    if _PLATFORM_ON_CLOUD_RE.search(norm) and re.search(r"\bannounc\w+", norm, re.I):
        return QUALITY_HARD

    # 运营商 × 云 AI：历史回测偏正，按硬催化
    if is_telco_cloud_deal(norm) and re.search(
        r"\b(?:partnership|agreement|collaborat\w*|deploy|ai)\b",
        norm,
        re.I,
    ):
        return QUALITY_HARD

    # 前沿实验室 × 具名 GPU 部署
    if is_frontier_gpu_deploy(norm):
        return QUALITY_HARD

    # 并购优先于「有金额→hard」：首日往往对不上纯并购溢价叙事
    if _MA.search(norm):
        return QUALITY_MA

    if is_product_only_integration(norm) or (
        (_MARKETPLACE_LISTING.search(norm) or _SOFT_LAUNCH.search(norm) or _GPU_DEPLOY_SOFT.search(norm))
        and not _COMMERCIAL_TERMS.search(norm)
        and not _PLATFORM_ON_CLOUD_RE.search(norm)
        and not is_frontier_gpu_deploy(norm)
    ):
        return QUALITY_SOFT_PRODUCT

    if _NAMED_PLATFORM.search(norm) and re.search(
        r"\b(?:anthropic|openai|claude|salesforce)\b",
        norm,
        re.I,
    ):
        return QUALITY_HARD

    if _HYPERSCALER.search(norm) and _POWER_STORAGE.search(norm) and re.search(
        r"\b(?:agreement|collaborat\w*|partnership|contract|provide|supply|deliver|deal|用电)\b",
        norm,
        re.I,
    ):
        return QUALITY_HARD

    if _COMMERCIAL_TERMS.search(norm):
        return QUALITY_HARD

    if _VAGUE_COOP.search(norm) and not _COMMERCIAL_TERMS.search(norm):
        return QUALITY_VAGUE
    return QUALITY_NORMAL


def score_materiality(text: str, source: str, matched_keywords: list[str]) -> int:
    norm = text.lower()
    score = 32
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

    soft_ai = any(tok in norm for tok in _AI_SOFT_ESCAPE)
    if soft_ai and re.search(
        r"integration|plugin|agentforce|claudeforce|copilot|product launch|announc",
        norm,
    ):
        if quality == QUALITY_SOFT_PRODUCT:
            score += 4
        elif quality == QUALITY_HARD:
            score += 16
        else:
            score += 8

    if quality == QUALITY_HARD and _HYPERSCALER.search(norm) and _POWER_STORAGE.search(norm):
        score += 20

    if source in ("pr_newswire", "globe", "sec_8k") or source.startswith(
        ("finnhub:", "google_news", "business_wire", "ir:")
    ):
        score += 5

    coop_verbs = ["signs", "signed", "enters", "awards", "awarded", "lease"]
    if any(v in norm for v in coop_verbs):
        score += 5

    if len(matched_keywords) >= 3:
        score += 4

    for kw in NEGATIVE_KEYWORDS:
        if kw.lower() in norm:
            if "mou" in kw.lower() or "memorandum" in kw.lower() or "谅解" in kw:
                score -= 22
            elif "non-binding" in kw.lower() or "非约束" in kw:
                score -= 25
            elif "research" in kw.lower() or "academic" in kw.lower():
                score -= 30
            else:
                score -= 15

    if quality == QUALITY_VAGUE:
        score -= 18
    elif "strategic partnership" in norm and not amount_match and not soft_ai:
        score -= 20

    if quality == QUALITY_FINANCING:
        score -= 22
    elif quality == QUALITY_MA:
        score -= 14
    elif quality == QUALITY_SOFT_PRODUCT:
        score -= 18

    if _MINING_PIVOT.search(text or ""):
        score -= 20

    return max(0, min(100, score))


def finalize_materiality_score(
    text: str,
    source: str,
    matched_keywords: list[str],
    *,
    llm_score: int | None = None,
    event_type: str | None = None,
) -> int:
    """规则分 + LLM 分合并；按催化硬度封顶，避免软整合/空话被打到 80+。"""
    base = score_materiality(text, source, matched_keywords)
    quality = classify_deal_quality(text)
    score = base
    et = (event_type or "").strip()
    commercial = has_commercial_terms(text)

    if llm_score:
        if quality == QUALITY_SOFT_PRODUCT:
            score = min(base + 4, int(llm_score), 52)
        elif quality == QUALITY_VAGUE:
            score = min(base + 3, int(llm_score), 48)
        elif quality == QUALITY_FINANCING:
            score = min(max(base, min(int(llm_score), base + 4)), 55)
        elif quality == QUALITY_MA:
            score = min(max(base, min(int(llm_score), base + 8)), 65)
        elif et == "ai_platform_deal":
            score = min(100, max(base, min(int(llm_score), base + 10)))
            if quality != QUALITY_HARD and not commercial:
                score = min(score, 58)
        else:
            bump = min(int(llm_score), base + (10 if commercial else 6))
            score = min(100, max(base, bump))
            if not commercial and quality == QUALITY_NORMAL:
                score = min(score, 58)

    from app.deal_monitor.keywords import _PLATFORM_ON_CLOUD_RE

    if _PLATFORM_ON_CLOUD_RE.search(text or "") and re.search(r"\bannounc\w+", text or "", re.I):
        score = max(score, 75)

    if re.search(r"\bdesign\s+win\b", text or "", re.I) and not re.search(
        r"\$[\d,.]+\s*billion|\bgigawatt\b|multi-year", text or "", re.I
    ):
        score = min(score, 55)

    if quality == QUALITY_SOFT_PRODUCT:
        score = min(score, 48)
    elif quality == QUALITY_VAGUE:
        score = min(score, 48)
    elif quality == QUALITY_FINANCING:
        score = min(score, 55)
    elif quality == QUALITY_MA:
        score = min(score, 65 if commercial else 58)
    elif quality == QUALITY_NORMAL and not commercial:
        score = min(score, 55)

    if score >= 70 and quality != QUALITY_HARD and not commercial:
        score = min(score, 58)

    # 硬催化地板放在封顶之后，避免被 vague/normal 盖掉
    if quality == QUALITY_HARD and _HYPERSCALER.search(text or "") and _POWER_STORAGE.search(
        text or ""
    ):
        # 公用事业用电合同：首日弹性常一般
        if re.search(
            r"用电合同|large[- ]load|georgia\s+power|utility\s+(?:power|agreement)",
            text or "",
            re.I,
        ) and not re.search(r"\$[\d,.]+\s*billion|\bgigawatt\b", text or "", re.I):
            score = min(max(score, 60), 62)
        else:
            score = max(score, 78)

    if _NAMED_PLATFORM.search(text or "") and re.search(
        r"\b(?:anthropic|openai|claude|salesforce)\b", text or "", re.I
    ):
        score = max(score, 72)

    if is_telco_cloud_deal(text or ""):
        score = max(score, 70)

    if is_frontier_gpu_deploy(text or ""):
        score = max(score, 68)

    return max(0, min(100, int(score)))

def score_outcome_gap(materiality: int | None, first_day_score: int | None) -> int | None:
    if materiality is None or first_day_score is None:
        return None
    return int(materiality) - int(first_day_score)


def is_large_score_outcome_gap(
    materiality: int | None,
    first_day_score: int | None,
    *,
    threshold: int = SCORE_OUTCOME_GAP,
) -> bool:
    gap = score_outcome_gap(materiality, first_day_score)
    return gap is not None and abs(gap) >= threshold


def is_weak_quality_for_display(quality: str) -> bool:
    return quality in {
        QUALITY_SOFT_PRODUCT,
        QUALITY_FINANCING,
        QUALITY_VAGUE,
    }


def should_soft_skip_push(quality: str) -> bool:
    """弱催化 / 融资 / 空话 / 并购：可入库对照，默认不推送。"""
    return quality in {
        QUALITY_SOFT_PRODUCT,
        QUALITY_FINANCING,
        QUALITY_VAGUE,
        QUALITY_MA,
    }


def calibrate_score_toward_outcome(
    rule_score: int,
    first_day_score: int | None,
) -> int:
    """有首日回测时，虚高/虚低规则分往回测方向校准（批量重打分用）。"""
    if first_day_score is None:
        return rule_score
    gap = rule_score - first_day_score
    if gap >= SCORE_OUTCOME_GAP:
        # 虚高：更靠近回测，仍保留约 25% 规则溢价
        return max(0, min(100, int(round(first_day_score + gap * 0.25))))
    if gap <= -SCORE_OUTCOME_GAP:
        # 虚低：首日强（≥70）时更积极抬分，否则保留约 30% 差距
        pull = 0.72 if first_day_score >= 70 else 0.55
        return max(0, min(100, int(round(rule_score + (-gap) * pull))))
    return rule_score
