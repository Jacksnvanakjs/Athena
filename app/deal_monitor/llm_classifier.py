"""Gemini 批量识别：从 RSS/SEC 中筛出 AI 算力产业链合作稿。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx

from app.deal_monitor.config import DEAL_LLM_MODEL, GEMINI_API_KEY
from app.deal_monitor.fetchers.pr_wire import RawItem

logger = logging.getLogger(__name__)


@dataclass
class LlmDecision:
    source_url: str
    is_relevant: bool
    anchor_name: str | None = None
    beneficiary_name: str | None = None
    anchor_ticker: str | None = None
    beneficiary_ticker: str | None = None
    event_type: str = "compute_deal"
    llm_score: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# 通用主题 / 商业信号（提示词与规则兜底共用，避免只靠单个案例）
# ---------------------------------------------------------------------------

THEME_TOKENS = (
    # 算力容量
    "gpu", "accelerator", "ai cluster", "compute capacity", "training capacity",
    "inference capacity", "ai infrastructure", "hyperscale",
    # 数据中心 / 电力 / 液冷
    "data center", "data centre", "colocation", "colo ", "megawatt", "gigawatt",
    "mw capacity", "power purchase", "liquid cooling", "液冷", "数据中心", "算力",
    # 定制硅 / 芯片供应链
    "custom semiconductor", "custom silicon", "custom chip", "asic", "tpu",
    "ai inference", "inference accelerator", "near-memory", "hbm",
    "memory interface", "network interface", "ethernet", "infiniband",
    "optical", "光子", "foundry", "advanced packaging", "chiplet",
    "semiconductor products", "wafer",
    # SaaS / Agent / 大模型产品整合（Claudeforce 类）
    "ai agent", "agentic", "agentforce", "claudeforce", "llm",
    "large language model", "generative ai", "enterprise ai", "copilot",
    "claude", "gpt-4", "gpt-5", "model integration", "ai assistant",
    "product integration", "salesforce in claude",
)

# 强商业信号：有其一即可，不强制美元金额
COMMERCIAL_SIGNAL_TOKENS = (
    "item 1.01", "material definitive agreement", "definitive agreement",
    "commercial agreement", "entered into", "enter into",
    "purchase agreement", "supply agreement", "capacity agreement",
    "lease agreement", "master services agreement", "offtake",
    "warrant", "multi-year", "years", "megawatt", "gigawatt",
    "$", "million", "billion",
    # 软件/平台合作常见措辞
    "strategic partnership", "expanded partnership", "expand", "collaboration",
    "product integration", "integration", "plugin", "announce", "launches",
    "launching", "jointly",
)

# 明显非目标：融资/并购等（兜底时直接放弃）
HARD_NEGATIVE_TOKENS = (
    "notes offering", "senior notes", "credit agreement", "revolving credit",
    "term loan", "convertible note", "atm offering", "equity distribution",
    "merger agreement", "arrangement agreement", "stock purchase agreement",
    "securities purchase agreement", "private placement", "underwriting agreement",
    "indenture", "debenture",
)


def _summary_window(item: RawItem) -> str:
    # SEC Item 1.01 对方/条款常在中后段；RSS 摘要本身较短
    limit = 1600 if item.source == "sec_8k" else 800
    return (item.summary or "")[:limit]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(tok in text for tok in tokens)


def _has_counterparty_cue(text: str) -> bool:
    """摘要里是否像写了「与另一方」——通用，不绑具体公司名。"""
    patterns = (
        r"\band\s+[A-Z][A-Za-z0-9&.,' -]{1,60}\s*\(",
        r"\bwith\s+[A-Z][A-Za-z0-9&.,' -]{1,60}\b",
        r"\bbetween\s+.+\band\s+",
        r"entered into .{0,80}\bwith\b",
        r"commercial agreement .{0,80}\bwith\b",
        r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+LLC\b",
        r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+Inc\.?\b",
        r"\b[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)?\s+Corp\.?\b",
    )
    return any(re.search(p, text or "") for p in patterns)


def heuristic_ai_deal_signal(item: RawItem) -> bool:
    """
    高置信规则信号：用于 LLM 漏判兜底，不替代 LLM 主体判断。
    条件：主题命中 + 商业信号 + 对方线索，且非硬融资/并购。
    """
    blob = _norm(f"{item.headline}\n{item.summary}")
    if not blob.strip():
        return False
    if _has_any(blob, HARD_NEGATIVE_TOKENS) and not _has_any(
        blob,
        (
            "data center", "data centre", "gpu", "custom semiconductor", "asic", "tpu",
            "colocation", "ai agent", "agentforce", "claudeforce", "claude", "llm",
            "generative ai", "anthropic", "openai",
        ),
    ):
        # 纯融资/并购：直接否；若融资稿里同时写了 data center/GPU/AI 平台等，仍交给主题判断
        if not _has_any(blob, THEME_TOKENS):
            return False
    if _has_any(blob, HARD_NEGATIVE_TOKENS) and not _has_any(blob, THEME_TOKENS):
        return False
    if not _has_any(blob, THEME_TOKENS):
        return False
    if not _has_any(blob, COMMERCIAL_SIGNAL_TOKENS):
        return False
    # SEC 正文常有 Company + 对方；RSS 至少要有 with/and 类线索
    if item.source == "sec_8k":
        return _has_counterparty_cue(item.summary or item.headline) or "item 1.01" in blob
    return _has_counterparty_cue(f"{item.headline}\n{item.summary}")


def _build_prompt(items: list[RawItem]) -> str:
    payload = []
    for idx, item in enumerate(items, start=1):
        payload.append(
            {
                "id": idx,
                "source_url": item.source_url,
                "source": item.source,
                "headline": item.headline,
                "summary": _summary_window(item),
            }
        )

    return (
        "你是美股「AI 产业链」材料性商业合作筛选器（含算力供给链 + 企业 AI 平台合作）。"
        "只根据标题和摘要判断，禁止脑补未出现的金额/对方/条款。\n\n"
        "用三道闸门决定 relevant（必须全过才 true）：\n"
        "闸门1 双方：存在两家不同公司（申报方/买方/卖方/合作方）。"
        "只有 Item 标题、无对方名 → false。\n"
        "闸门2 主题：属于下方「主题白名单」任一子类；不属于则 false。"
        "不要把主题窄化成「只有 GPU 租赁/机柜 MW」。\n"
        "闸门3 商业信号：有材料性合同/产品落地信号（不必有美元金额）。"
        "Item 1.01 / definitive|commercial|supply|capacity|lease agreement / "
        "entered into / multi-year / MW|GW|数量 / 与采购挂钩的 warrant|equity / "
        "正式产品整合上线（plugin/agent/integration launch）/ "
        "named frontier-model lab 的 expanded strategic partnership "
        "任一即可。纯仪式/认证/MOU/无产品落地的口头加速合作 → false。\n\n"
        "【主题白名单】\n"
        "T1 算力容量：GPU/加速器采购或租赁、集群、云/专用算力包年包容量\n"
        "T2 数据中心基建：托管/colo、机柜、电力/PPA、液冷，明确服务 AI/hyperscale\n"
        "T3 定制硅与芯片：ASIC/TPU/custom semiconductor|silicon|chip、"
        "inference accelerator、为云厂开发定制芯片、design win\n"
        "T4 AI 集群互联与光模块：以太网/InfiniBand/NIC/光互联/交换，明确 AI 训练/推理场景\n"
        "T5 AI 存储与先进封装：HBM、近存算、存储/内存控制器、foundry/advanced packaging "
        "且服务 AI 加速器\n"
        "T6 其他算力供应：明确写给 AI 训练/推理/智算用的长期供应或 offtake\n"
        "T7 企业 AI 平台合作：美股 SaaS/CRM/数据/安全软件公司与 OpenAI/Anthropic/Google/"
        "Microsoft 等大模型方，推出 Agent/Copilot/插件/工作流整合，或扩大战略合作且有"
        "明确产品名/上线计划（如 Claudeforce、Salesforce in Claude）。"
        "纯财报超预期、无新合作细节 → false；财报稿中若同时宣布上述产品合作 → true。\n\n"
        "【模式正例】（抽象模板）\n"
        "- 美股芯片/光模块/DC 公司 + 云厂/hyperscaler + 正式商业协议/Item 1.01 → true\n"
        "- 算力/托管商 + 云厂或大模型公司 + 多年容量/MW 协议 → true\n"
        "- 供应链公司 + 采购挂钩 warrant/长期供应，标的是 AI 芯片或加速器生态 → true\n"
        "- 美股 SaaS + Anthropic/OpenAI + 命名产品整合/Agent 上线 → true "
        "(event_type=ai_platform_deal)\n"
        "【模式负例】\n"
        "- 索引页仅 Item 1.01 无对方/无标的；仪式合作；validated/certified；"
        "机器人/消费电子；与大模型无关的普通软件功能更新；信贷/发债/并购/私募；"
        "无产品落地的咨询/营销战略合作；纯 ETF/杠杆产品发行 → false\n\n"
        "角色与打分：\n"
        "- anchor=更大/更核心方（常为云厂或大模型公司）；beneficiary=业务直接受益的美股公司；"
        "禁止把小子公司映射成综合集团母公司；受益方无美股 ticker → false。\n"
        "- llm_score：仪式/认证 0-40；弱合作 40-60；正式协议/命名产品整合且过三闸 ≥70；"
        "再有 warrant/金额/年限/容量 ≥80。\n"
        "- SEC 与通稿：过三闸时倾向 true，禁止因「没写美元」否决；"
        "不确定且非 T7 产品整合则 false。"
        "event_type：算力类用 compute_deal；T7 用 ai_platform_deal。\n\n"
        "只返回 JSON（不要 ```），格式：\n"
        '{"items":[{"source_url":"...","is_relevant":true,"anchor_name":"...","beneficiary_name":"...","anchor_ticker":"NVDA","beneficiary_ticker":"RIOT","event_type":"compute_deal","llm_score":78,"reason":"..."}]}\n\n'
        f"待分析新闻：\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _extract_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


LLM_BATCH_SIZE = 25


def _decisions_from_parsed(parsed, requested: list[RawItem]) -> dict[str, LlmDecision]:
    result: dict[str, LlmDecision] = {}
    items_list = []
    if isinstance(parsed, dict):
        items_list = parsed.get("items") or []
    elif isinstance(parsed, list):
        items_list = parsed

    for item in items_list:
        source_url = item.get("source_url")
        if not source_url:
            continue
        anchor_ticker = item.get("anchor_ticker")
        beneficiary_ticker = item.get("beneficiary_ticker")
        result[source_url] = LlmDecision(
            source_url=source_url,
            is_relevant=bool(item.get("is_relevant")),
            anchor_name=item.get("anchor_name") or None,
            beneficiary_name=item.get("beneficiary_name") or None,
            anchor_ticker=str(anchor_ticker).upper() if anchor_ticker else None,
            beneficiary_ticker=str(beneficiary_ticker).upper() if beneficiary_ticker else None,
            event_type=item.get("event_type") or "compute_deal",
            llm_score=max(0, min(100, int(item.get("llm_score") or 0))),
            reason=str(item.get("reason") or ""),
        )

    # 模型常只返回 relevant=true 的条目；未出现的视为已看过且不相关
    for raw in requested:
        if raw.source_url not in result:
            result[raw.source_url] = LlmDecision(
                source_url=raw.source_url,
                is_relevant=False,
                reason="LLM 未列为相关",
            )
    return result


def apply_heuristic_rescue(
    items: list[RawItem],
    decisions: dict[str, LlmDecision],
) -> dict[str, LlmDecision]:
    """LLM 判 false 但高置信规则命中时抬为 true，防止主题换皮后再漏。"""
    by_url = {it.source_url: it for it in items}
    rescued = 0
    for url, decision in list(decisions.items()):
        if decision.is_relevant and decision.llm_score >= 70:
            continue
        item = by_url.get(url)
        if not item or not heuristic_ai_deal_signal(item):
            continue
        # 保留 LLM 已抽出的双方；没有则交给 pipeline 的 SEC/实体解析
        decisions[url] = LlmDecision(
            source_url=url,
            is_relevant=True,
            anchor_name=decision.anchor_name,
            beneficiary_name=decision.beneficiary_name,
            anchor_ticker=decision.anchor_ticker,
            beneficiary_ticker=decision.beneficiary_ticker,
            event_type=decision.event_type or "compute_deal",
            llm_score=max(decision.llm_score, 72),
            reason=(
                f"规则兜底(主题+商业信号): {decision.reason}"
                if decision.reason
                else "规则兜底: AI产业链主题+材料性协议信号"
            ),
        )
        rescued += 1
    if rescued:
        logger.info("LLM 漏判规则兜底 %s 条", rescued)
    return decisions


async def _classify_batch(items: list[RawItem]) -> dict[str, LlmDecision] | None:
    models = [DEAL_LLM_MODEL, "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    data = None
    last_exc: Exception | None = None
    body = {
        "contents": [{"parts": [{"text": _build_prompt(items)}]}],
        "generationConfig": {
            "temperature": 0.1,
            "topP": 0.8,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=90) as client:
        for model in dict.fromkeys(models):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
            for attempt in range(2):
                try:
                    resp = await client.post(url, json=body)
                    if resp.status_code in (429, 503):
                        last_exc = httpx.HTTPStatusError(
                            f"{resp.status_code} {resp.text[:200]}",
                            request=resp.request,
                            response=resp,
                        )
                        if attempt == 0:
                            await asyncio.sleep(2)
                            continue
                        break
                    resp.raise_for_status()
                    data = resp.json()
                    break
                except Exception as exc:
                    last_exc = exc
                    break
            if data is not None:
                break

    if data is None:
        logger.warning("Gemini 分类失败: %s", last_exc)
        return None

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        logger.warning("Gemini 返回结构异常")
        return None

    parsed = _extract_json(text)
    if not parsed:
        logger.warning("Gemini JSON 解析失败")
        return None
    return _decisions_from_parsed(parsed, items)


async def classify_items(items: list[RawItem]) -> dict[str, LlmDecision]:
    """按批分类；失败的批次不写入结果，便于下一轮重试。成功后做规则兜底。"""
    if not GEMINI_API_KEY or not items:
        return {}

    merged: dict[str, LlmDecision] = {}
    for start in range(0, len(items), LLM_BATCH_SIZE):
        chunk = items[start : start + LLM_BATCH_SIZE]
        part = await _classify_batch(chunk)
        if part:
            merged.update(apply_heuristic_rescue(chunk, part))
    return merged
