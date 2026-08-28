"""公司名 / 文本片段 → 美股 ticker 解析（不走 LLM 猜代码）。"""

from __future__ import annotations

import re

from app.deal_monitor.config import FINNHUB_API_KEY
from app.deal_monitor.entities import Entity, registry
from app.deal_monitor.finnhub import search_symbol

# 种子库未覆盖时的补充映射（优先维护 entities_seed.json）
STATIC_TICKER_MAP = {
    "nvidia": "NVDA",
    "lg electronics": "LPLCY",
    "castrol": "BP",
    "bp": "BP",
    "intel": "INTC",
    "amd": "AMD",
    "ibm": "IBM",
    "cisco": "CSCO",
    "salesforce": "CRM",
    "snowflake": "SNOW",
    "palantir": "PLTR",
    "super micro": "SMCI",
    "supermicro": "SMCI",
    "coreweave": "CRWV",
    "riot platforms": "RIOT",
    "applied digital": "APLD",
    "digital realty": "DLR",
    "equinix": "EQIX",
    "verizon": "VZ",
    "visa": "V",
}

TICKER_IN_TEXT_RE = re.compile(
    r"\((?:NASDAQ|NYSE|AMEX|Nasdaq|Nyse):\s*([A-Z]{1,5})\)|\b(?:NASDAQ|NYSE|AMEX):\s*([A-Z]{1,5})\b",
    re.I,
)

# 「available through X, Y, and Z」类渠道名单语境
CHANNEL_CUE_RE = re.compile(
    r"\b(?:available|sold|distributed|offered|carried)\s+through\b|"
    r"\b(?:authorized\s+)?(?:reseller|distributor|channel\s+partner)s?\b",
    re.I,
)


def _normalize_name(name: str) -> str:
    name = name.strip()
    name = name.replace("&", " and ")
    name = re.sub(r"[^a-zA-Z0-9\s]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    lower = name.lower()
    suffixes = [
        " inc",
        " corp",
        " corporation",
        " ltd",
        " limited",
        " llc",
        " co",
        " company",
        " plc",
        " holdings",
        " holding",
        " technologies",
        " technology",
        " systems",
        " group",
    ]
    for suf in suffixes:
        if lower.endswith(suf):
            return name[: -len(suf)].strip()
    return name


def _name_variants(name: str) -> list[str]:
    name = name.strip()
    if not name:
        return []
    normalized = _normalize_name(name)
    variants = [name, normalized, normalized.replace(" ", "")]
    if normalized.lower() in STATIC_TICKER_MAP:
        variants.append(STATIC_TICKER_MAP[normalized.lower()])
    return list(dict.fromkeys(v for v in variants if v))


def extract_tickers_from_text(text: str) -> list[str]:
    found: list[str] = []
    for m in TICKER_IN_TEXT_RE.finditer(text):
        ticker = (m.group(1) or m.group(2) or "").upper()
        if ticker and ticker not in found:
            found.append(ticker)
    return found


def is_channel_partner_entity(entity: Entity, context: str = "") -> bool:
    """判断实体是否为渠道商（含 MSI 稿里裸写的 ASI）。"""
    if registry.is_channel_partner(entity.name, entity.ticker):
        return True
    norm = registry._normalize_alias_key(entity.name or "")
    if norm == "asi" and CHANNEL_CUE_RE.search(context):
        return True
    return False


def _lookup_seed(name: str) -> Entity | None:
    """entities_seed.json 为权威来源。"""
    registry.load_seed()
    hit = registry.lookup_name(name)
    if hit:
        return hit
    # 别名子串：如 "Marvell Technology Inc" → Marvell
    for entity in registry.extract_entities(name):
        if entity.unlisted_id:
            return Entity(name=name, unlisted_id=entity.unlisted_id)
        if entity.ticker:
            return Entity(name=name, ticker=entity.ticker.upper())
    return None


async def resolve_entity(name: str, context: str = "") -> Entity:
    """
    公司名 → 实体。解析顺序固定，不使用 LLM 提供的 ticker：
    1. entities_seed.json（含未上市锚点）
    2. 静态补充表
    3. 正文中的 (NYSE: XXX) 标注
    4. Finnhub 符号搜索（仅兜底）
    """
    name = (name or "").strip()
    if not name:
        return Entity(name="")

    hit = _lookup_seed(name)
    if hit:
        return hit

    if re.fullmatch(r"[A-Z]{1,5}", name.upper()):
        return Entity(name=name, ticker=name.upper())

    lower = _normalize_name(name).lower()
    if lower in STATIC_TICKER_MAP:
        return Entity(name=name, ticker=STATIC_TICKER_MAP[lower])

    for ticker in extract_tickers_from_text(f"{name}\n{context}"):
        return Entity(name=name, ticker=ticker)

    if FINNHUB_API_KEY:
        for variant in _name_variants(name):
            if re.fullmatch(r"[A-Z]{1,5}", variant.upper()):
                continue
            ticker = await search_symbol(variant, FINNHUB_API_KEY)
            if ticker:
                return Entity(name=name, ticker=ticker)

    return Entity(name=name)


def parse_sec_filer(headline: str) -> str | None:
    """从 SEC 8-K 标题提取申报公司名称。"""
    m = re.search(r"8-K\s*-\s*(.+?)\s*\(\d{10}\)", headline, re.I)
    if not m:
        return None
    return m.group(1).strip()
