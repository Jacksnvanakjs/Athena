"""Gemini 批量识别：从 RSS 中筛出 AI 算力/数据中心合作稿。"""

from __future__ import annotations

import asyncio
import json
import logging
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


def _build_prompt(items: list[RawItem]) -> str:
    payload = []
    for idx, item in enumerate(items, start=1):
        payload.append(
            {
                "id": idx,
                "source_url": item.source_url,
                "source": item.source,
                "headline": item.headline,
                "summary": item.summary[:600],
            }
        )

    return (
        "你是一个严格的美股 AI 产业链合作快讯筛选器。"
        "只根据标题和摘要判断，不要假设正文里还有未给出的金额或协议。"
        "目标是找出会对美股标的产生材料性影响的「新签/扩容」算力或数据中心商业协议。"
        "只保留真正存在两家不同公司、且有商业条款信号的事件。\n"
        "允许：新签或明确扩容的 GPU/算力容量、数据中心托管/租赁、电力或机柜 MW/GW 级协议，"
        "且受益方是美股可交易公司（不是未上市子公司硬映射到母公司）。\n"
        "一律 relevant=false：\n"
        "- 已有合作的仪式性新闻（高管到访、加速合作、战略合作展、产品联名、实验室揭幕）\n"
        "- 产品验证/认证/兼容性（validated / certified / compatible）但没有新的采购、租赁或容量合同\n"
        "- 子公司新闻，且该业务相对上市母公司明显偏小（如 Castrol vs BP、品牌部门 vs 综合集团）\n"
        "- 机器人/消费电子/普通软件合作，即使对方是 NVIDIA\n"
        "- 信贷、并购、药企授权、普通融资；8-K 只列出 Item 1.01 而无协议对方\n"
        "- MOU、探索性合作、学术研究、单方产品发布\n\n"
        "规则：\n"
        "1. relevant=true 必须同时满足：两家不同公司 + 主题是 AI 算力/数据中心基础设施 + 有材料性商业信号"
        "（金额、年限、MW/GW、GPU 数量、正式协议/lease/capacity agreement 至少一类）。\n"
        "2. anchor 是更大/更核心的一方；beneficiary 必须是其自身业务会受该协议直接影响的美股 ticker，"
        "禁止把小子公司映射成综合集团母公司。\n"
        "3. 若已知美股 ticker，输出 anchor_ticker / beneficiary_ticker。受益方无美股 ticker 则 relevant=false。\n"
        "4. llm_score 0-100：仪式/验证稿 0-40；有合作但条款弱 40-60；有金额/年限/容量的正式协议 70+。\n"
        "5. 不确定就 relevant=false。event_type 默认 compute_deal。\n\n"
        "只返回 JSON（不要带 ``` 或任何额外文本），格式为：\n"
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
    """按批分类；失败的批次不写入结果，便于下一轮重试。"""
    if not GEMINI_API_KEY or not items:
        return {}

    merged: dict[str, LlmDecision] = {}
    for start in range(0, len(items), LLM_BATCH_SIZE):
        chunk = items[start : start + LLM_BATCH_SIZE]
        part = await _classify_batch(chunk)
        if part:
            merged.update(part)
    return merged

