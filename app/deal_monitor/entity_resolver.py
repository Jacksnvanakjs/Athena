"""公司名 / 文本片段 → 美股 ticker 解析。"""

from __future__ import annotations

import re

from app.deal_monitor.config import FINNHUB_API_KEY
from app.deal_monitor.entities import Entity, registry
from app.deal_monitor.finnhub import search_symbol

# 常见公司 → 美股 ticker（ADR 或主上市代码）
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
}

TICKER_IN_TEXT_RE = re.compile(
    r"\((?:NASDAQ|NYSE|AMEX|Nasdaq|Nyse):\s*([A-Z]{1,5})\)|\b(?:NASDAQ|NYSE|AMEX):\s*([A-Z]{1,5})\b",
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


async def resolve_entity(name: str, context: str = "", ticker_hint: str | None = None) -> Entity:
    """解析公司实体：种子库 → 静态映射 → 文本 ticker → Finnhub。"""
    name = (name or "").strip()
    if not name and ticker_hint:
        return Entity(name=ticker_hint, ticker=ticker_hint.upper())

    if ticker_hint:
        ticker = ticker_hint.upper()
        seed = registry.extract_entities(name) if name else []
        display = seed[0].name if seed else name or ticker
        return Entity(name=display, ticker=ticker)

    # 名称本身就是 ticker
    if re.fullmatch(r"[A-Z]{1,5}", name.upper()):
        return Entity(name=name, ticker=name.upper())

    lower = _normalize_name(name).lower()
    if lower in STATIC_TICKER_MAP:
        return Entity(name=name, ticker=STATIC_TICKER_MAP[lower])

    for variant in _name_variants(name):
        seed = registry.extract_entities(variant)
        if seed:
            for entity in seed:
                if entity.ticker or entity.unlisted_id:
                    return Entity(
                        name=name,
                        ticker=entity.ticker,
                        unlisted_id=entity.unlisted_id,
                    )
            return Entity(name=name, ticker=seed[0].ticker, unlisted_id=seed[0].unlisted_id)

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
