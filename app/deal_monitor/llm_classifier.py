"""Gemini 批量识别：从 RSS/SEC 中筛出 AI 算力产业链合作稿。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

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
    beneficiary_names: list[str] = field(default_factory=list)
    anchor_ticker: str | None = None
    beneficiary_ticker: str | None = None
    event_type: str = "compute_deal"
    llm_score: int = 0
    reason: str = ""

    def all_beneficiary_names(self) -> list[str]:
        out: list[str] = []
        if self.beneficiary_name:
            out.append(self.beneficiary_name.strip())
        for name in self.beneficiary_names or []:
            n = (name or "").strip()
            if n and n not in out:
                out.append(n)
        return out


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
    # 云安全 / AI-native security 平台分销
    "ai-native security", "cybersecurity", "security platform", "falcon platform",
    "google cloud infrastructure", "available on google cloud",
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
        "【解读原则】像人一样读稿：很多通稿**表面是产品上架/now available/GA**，"
        "实质是 hyperscaler×ISV 的**战略合作、分销渠道、联合发布**。"
        "遇到此类稿：看是否双方联合宣布、是否改变渠道/部署方式/采购路径；"
        "若是 → relevant=true，reason 开头写「实质：…合作；表面：…产品」；"
        "若只是小版本功能更新、无新商业关系 → false。\n\n"
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
        "T7 企业 AI / 云安全平台合作：美股 SaaS/CRM/数据/安全软件公司与 OpenAI/Anthropic/Google/"
        "Microsoft/AWS 等云厂/hyperscaler，推出 Agent/Copilot/插件/工作流整合，"
        "**或核心企业安全/AI-native 平台正式运行于对方云基础设施**（如 Falcon on Google Cloud、"
        "Marketplace 分销），且有明确产品名/上线/区域计划。"
        "纯财报超预期、无新合作细节 → false；财报稿中若同时宣布上述产品合作 → true。\n\n"
        "【模式正例】（抽象模板）\n"
        "- 美股芯片/光模块/DC 公司 + 云厂/hyperscaler + 正式商业协议/Item 1.01 → true\n"
        "- 算力/托管商 + 云厂或大模型公司 + 多年容量/MW 协议 → true\n"
        "- 供应链公司 + 采购挂钩 warrant/长期供应，标的是 AI 芯片或加速器生态 → true\n"
        "- 美股 SaaS + Anthropic/OpenAI + 命名产品整合/Agent 上线 → true "
        "(event_type=ai_platform_deal)\n"
        "- CrowdStrike Falcon 平台正式运行于 Google Cloud 基础设施、双方 IR 联合宣布 → "
        "relevant=true；beneficiary_name=CrowdStrike，anchor_name=Google；"
        "event_type=ai_platform_deal；llm_score≥75\n"
        "- Anthropic 与未上市 Lambda 签 $35B 算力协议、得州数据中心由上市 Hut 8 建设、"
        "Nvidia 持租约 → relevant=true；beneficiary_name=Hut 8（或 HUT），anchor=NVIDIA 或 Anthropic\n"
        "【模式负例】\n"
        "- 索引页仅 Item 1.01 无对方/无标的；仪式合作；validated/certified；"
        "机器人/消费电子；与大模型无关的普通软件功能更新；信贷/发债/并购/私募；"
        "无产品落地的咨询/营销战略合作；纯 ETF/杠杆产品发行 → false\n"
        "- 纯产品功能/整合发布（deepens integration、integrates with、product launch、"
        "unveils、introduces、now available），无 definitive/commercial agreement、"
        "无 multi-year、无金额/容量条款 → false。"
        "**例外**：命名安全平台首次/重大运行于 hyperscaler 云基础设施且双方联合宣布（Falcon on GCP）→ true。"
        "例：安全厂商与芯片平台的 FortiAIGate/NVIDIA 产品整合、"
        "SaaS 常规 Copilot 插件更新 → false\n"
        "- 旧闻复述/股价反应稿 → false：标题侧重「shares/stock up/jump/soar X% "
        "after/following announcing partnership/deal」，而非今日新签署/新条款；"
        "低质 SEO 站（如 Mshale）、标题混无关剧集/随机串 → false。"
        "例：Reddit shares up 11% after announcing OpenAI partnership（2024 旧闻重发）→ false\n\n"
        "角色与打分：\n"
        "- anchor=叙事核心/更大一方（云厂、大模型、Nvidia 等）；可以是未上市公司。\n"
        "- beneficiary=最值得关注的**美股上市**标的，用于推送与交易；"
        "**不要**因为标题签约方未上市就判 relevant=false。\n"
        "- 若直接签约方未上市（如 Lambda、私有 Neocloud），从正文产业链中找**材料性受益**的美股："
        "数据中心开发商/矿企转 AI（Hut 8）、GPU 云（CRWV/NBIS）、电力/机柜/光模块等。\n"
        "- beneficiary_name=主推送标的；beneficiary_names=可选额外上市受益方（按重要性排序，最多 3 个）。\n"
        "- 只输出公司常用名（不要 ticker）；系统用种子库解析代码。\n"
        "- relevant=true 条件：能指出至少 1 个可映射的美股 beneficiary，且过三闸。\n"
        "- 若只能想到未上市直接方、正文无任何上市供应链受益 → relevant=false。\n"
        "- llm_score：仪式/认证 0-40；弱合作 40-60；正式协议/命名产品整合且过三闸 ≥70；"
        "再有 warrant/金额/年限/容量 ≥80；巨额多年算力/DC 协议 ≥85。\n"
        "- SEC 与通稿：过三闸时倾向 true，禁止因「没写美元」否决；"
        "不确定且非 T7 产品整合则 false。"
        "event_type：算力/DC/租赁用 compute_deal；T7 用 ai_platform_deal。\n\n"
        "只返回 JSON（不要 ```），格式：\n"
        '{"items":[{"source_url":"...","is_relevant":true,"anchor_name":"...","beneficiary_name":"...",'
        '"beneficiary_names":["..."],"event_type":"compute_deal","llm_score":85,"reason":"..."}]}\n\n'
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
        extra_names = item.get("beneficiary_names") or []
        if not isinstance(extra_names, list):
            extra_names = []
        primary = (item.get("beneficiary_name") or "").strip() or None
        names: list[str] = []
        if primary:
            names.append(primary)
        for n in extra_names:
            s = str(n).strip()
            if s and s not in names:
                names.append(s)
        result[source_url] = LlmDecision(
            source_url=source_url,
            is_relevant=bool(item.get("is_relevant")),
            anchor_name=item.get("anchor_name") or None,
            beneficiary_name=names[0] if names else None,
            beneficiary_names=names[1:] if len(names) > 1 else [],
            # ticker 不由 LLM 决定，入库前由 resolve_entity 解析
            anchor_ticker=None,
            beneficiary_ticker=None,
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
        from app.deal_monitor.content_filter import reject_deal_item
        from app.deal_monitor.keywords import is_product_only_integration

        if reject_deal_item(item)[0]:
            continue
        blob = f"{item.headline}\n{item.summary}"
        if is_product_only_integration(blob):
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
