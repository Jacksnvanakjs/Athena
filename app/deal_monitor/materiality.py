"""材料性打分 0～100。"""

import re

from app.deal_monitor.keywords import NEGATIVE_KEYWORDS, _AI_SOFT_ESCAPE


def score_materiality(text: str, source: str, matched_keywords: list[str]) -> int:
    norm = text.lower()
    score = 40  # 基础分：已通过关键词+合作词筛选

    # 加分
    amount_match = re.search(
        r"\$[\d,.]+\s*(million|billion|m\b|b\b)|[\d,.]+\s*(million|billion)\s+dollars",
        norm,
        re.I,
    )
    if amount_match:
        val_str = amount_match.group(0)
        score += 25 if "billion" in val_str or "b\b" in val_str else 15

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

    # 企业 AI 平台产品落地信号
    soft_ai = any(tok in norm for tok in _AI_SOFT_ESCAPE)
    if soft_ai and re.search(
        r"integration|plugin|agentforce|claudeforce|copilot|product launch|announc",
        norm,
    ):
        score += 18

    if source in ("pr_newswire", "globe", "sec_8k") or source.startswith(
        ("finnhub:", "google_news")
    ):
        score += 8

    coop_verbs = ["signs", "signed", "enters", "awards", "awarded", "lease", "collaboration"]
    if any(v in norm for v in coop_verbs):
        score += 5

    if len(matched_keywords) >= 3:
        score += 5

    # 减分
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

    return max(0, min(100, score))
