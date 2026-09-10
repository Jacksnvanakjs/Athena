"""从 Finnhub 全市场财报日历发现 AI 相关标的（补固定名单漏洞）。

策略：
1. 固定种子名单（earnings_universe.json）始终监控
2. Finnhub 全市场 earnings calendar（按日期，不按 ticker）
3. 种子同行扩展 +「未知票 peers ∩ 种子」反向判定（可抓到 NTSK 等新 IPO）
4. profile 行业/关键词兜底
5. 持久化到 data/earnings_discovered.json，下次直接并入宇宙
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from app.config import DATA_DIR, FINNHUB_API_KEY
from app.earnings_monitor.calendar_fetch import (
    CalendarHit,
    _parse_date,
    _parse_session,
    _today_fetch_start,
)
from app.earnings_monitor.config import EARNINGS_LOOKAHEAD_DAYS
from app.earnings_monitor.universe import UniverseTicker

logger = logging.getLogger(__name__)

DISCOVERED_FILE = DATA_DIR / "earnings_discovered.json"

_PEER_SEEDS = (
    "CRWD", "ZS", "OKTA", "PANW", "S", "FTNT", "NET", "RPD", "TENB", "QLYS",
    "NVDA", "AMD", "MRVL", "AVGO", "SMCI", "ARM",
    "DDOG", "SNOW", "PLTR", "NOW", "CRM", "MDB",
    "ANET", "CSCO", "NTAP", "PSTG",
)

# 反向 peers 判定用的锚点（含 T0，只用于相关性，不代表一定入库推送）
_AI_ANCHORS = frozenset(_PEER_SEEDS) | {
    "MSFT", "GOOGL", "GOOG", "AMZN", "META", "ORCL", "AAPL", "IBM",
}

_AI_TEXT = re.compile(
    r"(?:"
    r"\bai\b|artificial\s+intelligence|machine\s+learning|generative\s+ai|"
    r"cyber\s*security|cybersecurity|cloud\s+security|zero[\s-]?trust|"
    r"identity\s+(?:security|access)|endpoint\s+security|saas\s+security|"
    r"semiconductor|gpu|graphics\s+processing|data\s+center|datacenter|"
    r"\bsaas\b|software[\s-]as[\s-]a[\s-]service|cloud\s+(?:platform|software)|"
    r"networking|ethernet|optical|photonic|cpo|"
    r"llm|large\s+language|inference|ai\s+infra|"
    r"网络安全|人工智能|半导体|数据中心"
    r")",
    re.I,
)

_SEC = re.compile(r"cyber|security|identity|zero[\s-]?trust|firewall|endpoint|secure", re.I)
_SEMI = re.compile(r"semi|chip|gpu|foundry|wafer|photonic|optical|memory|nand|dram", re.I)
_INFRA = re.compile(r"data\s*center|datacenter|hosting|colocation|mining|power|cooling|server", re.I)
_NET = re.compile(r"network|ethernet|switch|router|cpo|optical\s+transceiver", re.I)


def _classify_sector(blob: str) -> str:
    if _SEC.search(blob):
        return "AI_SEC"
    if _SEMI.search(blob):
        return "AI_SEMI"
    if _INFRA.search(blob):
        return "AI_INFRA"
    if _NET.search(blob):
        return "AI_NET"
    return "AI_SAAS"


def _is_ai_blob(blob: str) -> bool:
    return bool(_AI_TEXT.search(blob or ""))


def load_discovered() -> list[UniverseTicker]:
    if not DISCOVERED_FILE.is_file():
        return []
    try:
        data = json.loads(DISCOVERED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    out: list[UniverseTicker] = []
    seen: set[str] = set()
    for item in data.get("tickers") or []:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        out.append(
            UniverseTicker(
                ticker=ticker,
                name=str(item.get("name") or ticker).strip(),
                sector=str(item.get("sector") or "AI_SAAS").strip().upper(),
            )
        )
    return out


def save_discovered(items: list[UniverseTicker], *, sources: dict[str, str] | None = None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    existing = {u.ticker: {"ticker": u.ticker, "name": u.name, "sector": u.sector, "source": "discovered"} for u in load_discovered()}
    for u in items:
        existing[u.ticker] = {
            "ticker": u.ticker,
            "name": u.name,
            "sector": u.sector,
            "source": (sources or {}).get(u.ticker, "discovered"),
        }
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tickers": sorted(existing.values(), key=lambda x: x["ticker"]),
    }
    DISCOVERED_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def fetch_finnhub_market_earnings(
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[CalendarHit]:
    if not FINNHUB_API_KEY:
        return []
    start = from_date or _today_fetch_start()
    end = to_date or (start + timedelta(days=EARNINGS_LOOKAHEAD_DAYS))
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {"from": start.isoformat(), "to": end.isoformat(), "token": FINNHUB_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning("Finnhub market earnings HTTP %s", resp.status_code)
                return []
            rows = resp.json().get("earningsCalendar") or []
    except Exception as exc:
        logger.warning("Finnhub market earnings 失败: %s", exc)
        return []

    best: dict[str, CalendarHit] = {}
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym or not re.fullmatch(r"[A-Z]{1,5}", sym):
            continue
        ed = _parse_date(row.get("date"))
        if not ed or ed < start or ed > end:
            continue
        session = _parse_session(str(row.get("hour") or ""))
        hit = CalendarHit(
            ticker=sym,
            earnings_date=ed,
            session=session,
            confirmed=session in ("BMO", "AMC"),
            source="finnhub",
        )
        prev = best.get(sym)
        if not prev or ed < prev.earnings_date:
            best[sym] = hit
    return list(best.values())


async def _fetch_peers(client: httpx.AsyncClient, symbol: str, grouping: str = "industry") -> list[str]:
    url = "https://finnhub.io/api/v1/stock/peers"
    try:
        resp = await client.get(
            url,
            params={"symbol": symbol, "grouping": grouping, "token": FINNHUB_API_KEY},
        )
        if resp.status_code == 429:
            await asyncio.sleep(1.5)
            resp = await client.get(
                url,
                params={"symbol": symbol, "grouping": grouping, "token": FINNHUB_API_KEY},
            )
        if resp.status_code != 200:
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        return [str(x).strip().upper() for x in data if re.fullmatch(r"[A-Z]{1,5}", str(x).strip().upper() or "")]
    except Exception:
        return []


async def _fetch_profile(client: httpx.AsyncClient, symbol: str) -> dict:
    url = "https://finnhub.io/api/v1/stock/profile2"
    try:
        resp = await client.get(url, params={"symbol": symbol, "token": FINNHUB_API_KEY})
        if resp.status_code == 429:
            await asyncio.sleep(1.5)
            resp = await client.get(url, params={"symbol": symbol, "token": FINNHUB_API_KEY})
        if resp.status_code != 200:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _sector_for_peer_seed(seed: str) -> str:
    if seed in {"CRWD", "ZS", "OKTA", "PANW", "S", "FTNT", "NET", "RPD", "TENB", "QLYS"}:
        return "AI_SEC"
    if seed in {"NVDA", "AMD", "MRVL", "AVGO", "ARM"}:
        return "AI_SEMI"
    if seed in {"SMCI"}:
        return "AI_INFRA"
    if seed in {"ANET", "CSCO"}:
        return "AI_NET"
    return "AI_SAAS"


async def expand_peer_universe(known: set[str]) -> dict[str, UniverseTicker]:
    out: dict[str, UniverseTicker] = {}
    sem = asyncio.Semaphore(3)

    async with httpx.AsyncClient(timeout=25) as client:

        async def _one(sym: str) -> None:
            async with sem:
                peers_i = await _fetch_peers(client, sym, "industry")
                await asyncio.sleep(0.15)
                peers_s = await _fetch_peers(client, sym, "sector")
            sector = _sector_for_peer_seed(sym)
            for p in set(peers_i + peers_s):
                if p in known or p in out:
                    continue
                out[p] = UniverseTicker(ticker=p, name=p, sector=sector)

        await asyncio.gather(*[_one(s) for s in _PEER_SEEDS])
    return out


async def discover_ai_from_market(
    seed: list[UniverseTicker],
    *,
    max_checks: int = 100,
) -> tuple[list[UniverseTicker], dict]:
    """全市场财报日历 → AI 相关新标的。"""
    stats = {
        "market_symbols": 0,
        "peer_expanded": 0,
        "adjacent_added": 0,
        "profile_added": 0,
        "total_new": 0,
    }
    if not FINNHUB_API_KEY:
        return [], stats

    seed_map = {u.ticker: u for u in seed}
    known = set(seed_map) | {u.ticker for u in load_discovered()}
    anchors = set(_AI_ANCHORS) | known

    # 含近 7 日已公布：便于当周漏网新股仍被发现并归档对照
    start = _today_fetch_start() - timedelta(days=7)
    end = start + timedelta(days=EARNINGS_LOOKAHEAD_DAYS + 7)
    market_hits = await fetch_finnhub_market_earnings(start, end)
    market_syms = {h.ticker for h in market_hits}
    stats["market_symbols"] = len(market_syms)

    new_items: dict[str, UniverseTicker] = {}
    sources: dict[str, str] = {}

    # 1) 种子同行扩展（进入监控池，不限于本周有财报）
    peers = await expand_peer_universe(known)
    for ticker, item in peers.items():
        new_items[ticker] = item
        sources[ticker] = "finnhub_peers"
    stats["peer_expanded"] = len(peers)

    # 2) 日历未知票：peers ∩ AI 锚点 → 纳入（覆盖 NTSK 等新股）
    candidates = sorted(market_syms - known - set(new_items))
    sem = asyncio.Semaphore(3)
    checks = 0
    adjacent_n = 0
    profile_n = 0

    async with httpx.AsyncClient(timeout=25) as client:

        async def _adjacent(sym: str) -> tuple[UniverseTicker, str] | None:
            nonlocal checks
            async with sem:
                if checks >= max_checks:
                    return None
                checks += 1
                peers_i = await _fetch_peers(client, sym, "industry")
                await asyncio.sleep(0.12)
                peers_s = await _fetch_peers(client, sym, "sector")
                profile = await _fetch_profile(client, sym)
            peer_set = set(peers_i) | set(peers_s)
            name = str(profile.get("name") or sym)
            industry = str(profile.get("finnhubIndustry") or "")
            web = str(profile.get("weburl") or "")
            blob = f"{name} {industry} {web}"
            if peer_set & anchors:
                return (
                    UniverseTicker(
                        ticker=sym,
                        name=name,
                        sector=_classify_sector(blob + " " + " ".join(peer_set)),
                    ),
                    "finnhub_adjacent",
                )
            ind = industry.lower()
            if ind in {"semiconductors", "software"} or _is_ai_blob(blob):
                if ind == "technology" and not _is_ai_blob(blob):
                    return None
                return (
                    UniverseTicker(ticker=sym, name=name, sector=_classify_sector(blob)),
                    "finnhub_profile",
                )
            return None

        parts = await asyncio.gather(*[_adjacent(s) for s in candidates])
        for part in parts:
            if not part:
                continue
            item, src = part
            if item.ticker in known or item.ticker in new_items:
                continue
            new_items[item.ticker] = item
            sources[item.ticker] = src
            if src == "finnhub_adjacent":
                adjacent_n += 1
            else:
                profile_n += 1

    stats["adjacent_added"] = adjacent_n
    stats["profile_added"] = profile_n
    stats["total_new"] = len([t for t in new_items if t not in known])

    discovered = [u for t, u in new_items.items() if t not in known]
    if discovered:
        save_discovered(discovered, sources=sources)
        logger.info("earnings AI discovery: %s", stats)
    return discovered, stats
