"""美股板块/个股热力图数据（CNBC 批量报价）。

资金流入用「涨跌幅 × 成交额」作为代理指标：
正值偏流入/上涨，负值偏流出/下跌。真实 Level2 资金流需付费数据。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CNBC_QUOTE = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SPARK = "https://query1.finance.yahoo.com/v7/finance/spark"
EASTMONEY_ULIST = "https://push2.eastmoney.com/api/qt/ulist.np/get"

# 成功样本过少时不覆盖界面（避免「全 0」或「单板块 100%」）
_MIN_QUOTE_RATIO = 0.55
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None, "last_good": None}
_CACHE_TTL = 120  # 秒
_LAST_GOOD_TTL = 6 * 3600  # 拉取失败时最多回退 6 小时内的成功数据


# 11 大 GICS 板块 → SPDR 行业 ETF + 代表性龙头股
SECTORS: list[dict[str, Any]] = [
    {
        "key": "technology",
        "name": "科技",
        "etf": "XLK",
        "companies": [
            ("AAPL", "苹果"),
            ("MSFT", "微软"),
            ("NVDA", "英伟达"),
            ("AVGO", "博通"),
            ("CRM", "Salesforce"),
            ("AMD", "AMD"),
            ("ORCL", "甲骨文"),
            ("ADBE", "Adobe"),
        ],
    },
    {
        "key": "communication",
        "name": "通信服务",
        "etf": "XLC",
        "companies": [
            ("META", "Meta"),
            ("GOOGL", "谷歌"),
            ("NFLX", "奈飞"),
            ("DIS", "迪士尼"),
            ("TMUS", "T-Mobile"),
            ("VZ", "威瑞森"),
        ],
    },
    {
        "key": "consumer-discretionary",
        "name": "可选消费",
        "etf": "XLY",
        "companies": [
            ("AMZN", "亚马逊"),
            ("TSLA", "特斯拉"),
            ("HD", "家得宝"),
            ("MCD", "麦当劳"),
            ("NKE", "耐克"),
            ("SBUX", "星巴克"),
        ],
    },
    {
        "key": "financials",
        "name": "金融",
        "etf": "XLF",
        "companies": [
            ("BRK.B", "伯克希尔"),
            ("JPM", "摩根大通"),
            ("V", "Visa"),
            ("MA", "万事达"),
            ("BAC", "美国银行"),
            ("GS", "高盛"),
        ],
    },
    {
        "key": "health-care",
        "name": "医疗保健",
        "etf": "XLV",
        "companies": [
            ("LLY", "礼来"),
            ("UNH", "联合健康"),
            ("JNJ", "强生"),
            ("ABBV", "艾伯维"),
            ("MRK", "默沙东"),
            ("PFE", "辉瑞"),
        ],
    },
    {
        "key": "industrials",
        "name": "工业",
        "etf": "XLI",
        "companies": [
            ("GE", "通用电气"),
            ("CAT", "卡特彼勒"),
            ("RTX", "雷神"),
            ("HON", "霍尼韦尔"),
            ("UNP", "联合太平洋"),
            ("BA", "波音"),
        ],
    },
    {
        "key": "energy",
        "name": "能源",
        "etf": "XLE",
        "companies": [
            ("XOM", "埃克森美孚"),
            ("CVX", "雪佛龙"),
            ("COP", "康菲"),
            ("SLB", "斯伦贝谢"),
            ("EOG", "EOG"),
        ],
    },
    {
        "key": "consumer-staples",
        "name": "必需消费",
        "etf": "XLP",
        "companies": [
            ("WMT", "沃尔玛"),
            ("PG", "宝洁"),
            ("COST", "好市多"),
            ("KO", "可口可乐"),
            ("PEP", "百事"),
        ],
    },
    {
        "key": "utilities",
        "name": "公用事业",
        "etf": "XLU",
        "companies": [
            ("NEE", "新纪元能源"),
            ("SO", "南方电力"),
            ("DUK", "杜克能源"),
            ("CEG", "Constellation"),
        ],
    },
    {
        "key": "real-estate",
        "name": "房地产",
        "etf": "XLRE",
        "companies": [
            ("PLD", "普洛斯"),
            ("AMT", "美国电塔"),
            ("EQIX", "Equinix"),
            ("SPG", "西蒙地产"),
        ],
    },
    {
        "key": "materials",
        "name": "原材料",
        "etf": "XLB",
        "companies": [
            ("LIN", "林德"),
            ("SHW", "宣伟"),
            ("APD", "空气产品"),
            ("FCX", "自由港"),
            ("NEM", "纽蒙特"),
        ],
    },
]

PERIODS: dict[str, dict[str, Any]] = {
    # min_snapshots: 至少需要多少个交易日快照才展示该周期，否则留空
    "1d": {"days": 1, "label": "每天", "min_snapshots": 1},
    "1w": {"days": 7, "label": "每周", "min_snapshots": 5},
    "15d": {"days": 15, "label": "每半月", "min_snapshots": 10},
    "1m": {"days": 30, "label": "每月", "min_snapshots": 20},
    "2m": {"days": 60, "label": "每2个月", "min_snapshots": 40},
    "3m": {"days": 90, "label": "每3个月", "min_snapshots": 55},
}


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "N/A", "null"}:
        return None
    text = text.replace(",", "").replace("%", "").replace("+", "")
    # 支持 4.559T / 38.9M / 64.57M 这类缩写
    mult = 1.0
    if text[-1:].upper() == "T":
        mult = 1e12
        text = text[:-1]
    elif text[-1:].upper() == "B":
        mult = 1e9
        text = text[:-1]
    elif text[-1:].upper() == "M":
        mult = 1e6
        text = text[:-1]
    elif text[-1:].upper() == "K":
        mult = 1e3
        text = text[:-1]
    try:
        return float(text) * mult
    except ValueError:
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return float(match.group()) if match else None


def _quote_row(
    symbol: str,
    *,
    name: str,
    price: float,
    change_pct: float,
    volume: float,
    market_cap: float | None = None,
) -> dict[str, Any]:
    dollar_volume = price * volume
    return {
        "symbol": symbol,
        "name": name or symbol,
        "price": round(price, 2),
        "change_pct": round(change_pct, 2),
        "volume": int(volume),
        "dollar_volume": round(dollar_volume, 0),
        "flow_score": round(change_pct * dollar_volume / 1e9, 4),
        "market_cap": market_cap,
    }


def _parse_quote(raw: dict[str, Any]) -> dict[str, Any] | None:
    if raw.get("code") not in (0, "0", None):
        # code=1 means not found
        if raw.get("last") in (None, "", "0.00") and not raw.get("name"):
            return None
    price = _to_float(raw.get("last"))
    if price is None:
        return None
    change_pct = _to_float(raw.get("change_pct")) or 0.0
    volume = _to_float(raw.get("volume")) or 0.0
    return _quote_row(
        raw.get("symbol") or raw.get("shortName") or "",
        name=raw.get("name") or raw.get("onAirName") or raw.get("symbol") or "",
        price=price,
        change_pct=change_pct,
        volume=volume,
        market_cap=_to_float(raw.get("mktcapView")),
    )


async def _fetch_cnbc(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """CNBC 批量报价；部分云主机 IP 会被拦截，失败时返回空。"""
    out: dict[str, dict[str, Any]] = {}
    chunk_size = 40
    headers = {**HEADERS, "Referer": "https://www.cnbc.com/"}
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            try:
                resp = await client.get(
                    CNBC_QUOTE,
                    params={
                        "symbols": "|".join(chunk),
                        "requestMethod": "itv",
                        "noform": "1",
                        "partnerId": "2",
                        "fund": "1",
                        "exthrs": "1",
                        "output": "json",
                        "events": "1",
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
                quotes = (
                    (payload.get("FormattedQuoteResult") or {}).get("FormattedQuote")
                    or []
                )
                if isinstance(quotes, dict):
                    quotes = [quotes]
                for raw in quotes:
                    parsed = _parse_quote(raw)
                    if parsed and parsed.get("symbol"):
                        out[parsed["symbol"]] = parsed
            except Exception as exc:
                logger.warning("CNBC batch failed: %s", exc)
    return out


def _yahoo_symbol(symbol: str) -> str:
    # Yahoo 用 BRK-B，本站内部用 BRK.B
    return symbol.replace(".", "-")


def _from_yahoo_symbol(yahoo_symbol: str, wanted: set[str]) -> str:
    if yahoo_symbol in wanted:
        return yahoo_symbol
    dotted = yahoo_symbol.replace("-", ".")
    if dotted in wanted:
        return dotted
    return yahoo_symbol


def _parse_yahoo_spark_item(
    item: dict[str, Any], wanted: set[str]
) -> tuple[str, dict[str, Any]] | None:
    yahoo_sym = item.get("symbol") or ""
    resp = (item.get("response") or [{}])[0] or {}
    meta = resp.get("meta") or {}
    quote = ((resp.get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (quote.get("close") or []) if c is not None]
    volumes = [v for v in (quote.get("volume") or []) if v is not None]
    price = _to_float(meta.get("regularMarketPrice"))
    if price is None and closes:
        price = float(closes[-1])
    if price is None:
        return None
    change_pct = 0.0
    if len(closes) >= 2 and closes[-2]:
        change_pct = (float(closes[-1]) - float(closes[-2])) / float(closes[-2]) * 100
        price = float(closes[-1])
    else:
        prev = _to_float(meta.get("previousClose")) or _to_float(
            meta.get("chartPreviousClose")
        )
        if prev:
            change_pct = (price - prev) / prev * 100
    volume = float(volumes[-1]) if volumes else float(meta.get("regularMarketVolume") or 0)
    symbol = _from_yahoo_symbol(str(yahoo_sym), wanted)
    return symbol, _quote_row(
        symbol,
        name=meta.get("longName") or meta.get("shortName") or symbol,
        price=price,
        change_pct=change_pct,
        volume=volume,
    )


async def _fetch_yahoo_spark(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Yahoo spark 批量报价：一次最多约 20 只，云端比逐只 chart 稳得多。"""
    out: dict[str, dict[str, Any]] = {}
    wanted = set(symbols)
    headers = {**HEADERS, "Referer": "https://finance.yahoo.com/"}
    chunk_size = 20
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i : i + chunk_size]
            ysyms = [_yahoo_symbol(s) for s in chunk]
            try:
                resp = await client.get(
                    YAHOO_SPARK,
                    params={
                        "symbols": ",".join(ysyms),
                        "range": "5d",
                        "interval": "1d",
                    },
                )
                if resp.status_code != 200:
                    logger.warning("Yahoo spark HTTP %s", resp.status_code)
                    continue
                for item in (resp.json().get("spark") or {}).get("result") or []:
                    parsed = _parse_yahoo_spark_item(item, wanted)
                    if parsed:
                        sym, row = parsed
                        out[sym] = row
            except Exception as exc:
                logger.warning("Yahoo spark batch failed: %s", exc)
    return out


async def _fetch_yahoo_one(
    client: httpx.AsyncClient, symbol: str
) -> tuple[str, dict[str, Any] | None]:
    ysym = _yahoo_symbol(symbol)
    try:
        resp = await client.get(
            YAHOO_CHART.format(symbol=ysym),
            params={"interval": "1d", "range": "5d"},
        )
        if resp.status_code != 200:
            return symbol, None
        result = ((resp.json().get("chart") or {}).get("result")) or []
        if not result:
            return symbol, None
        fake = {"symbol": ysym, "response": [result[0]]}
        parsed = _parse_yahoo_spark_item(fake, {symbol, ysym, symbol.replace(".", "-")})
        if not parsed:
            return symbol, None
        return symbol, parsed[1]
    except Exception as exc:
        logger.debug("Yahoo quote failed %s: %s", symbol, exc)
        return symbol, None


async def _fetch_yahoo_chart_missing(
    symbols: list[str], existing: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    import asyncio

    missing = [s for s in symbols if s not in existing]
    if not missing:
        return {}
    out: dict[str, dict[str, Any]] = {}
    headers = {**HEADERS, "Referer": "https://finance.yahoo.com/"}
    sem = asyncio.Semaphore(5)
    async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=True) as client:

        async def one(sym: str):
            async with sem:
                return await _fetch_yahoo_one(client, sym)

        results = await asyncio.gather(*[one(s) for s in missing])
    for sym, row in results:
        if row:
            out[sym] = row
    return out


async def _fetch_yahoo(symbols: list[str]) -> dict[str, dict[str, Any]]:
    out = await _fetch_yahoo_spark(symbols)
    if len(out) < len(symbols):
        out.update(await _fetch_yahoo_chart_missing(symbols, out))
    return out


async def _fetch_eastmoney(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """东方财富美股列表接口（部分环境可用）。"""
    out: dict[str, dict[str, Any]] = {}
    headers = {**HEADERS, "Referer": "https://quote.eastmoney.com/"}
    markets = (105, 106, 107)
    async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
        for mkt in markets:
            missing = [s for s in symbols if s not in out]
            if not missing:
                break
            for i in range(0, len(missing), 20):
                chunk = missing[i : i + 20]
                secids = ",".join(f"{mkt}.{s}" for s in chunk)
                try:
                    resp = await client.get(
                        EASTMONEY_ULIST,
                        params={
                            "fltt": "2",
                            "secids": secids,
                            "fields": "f12,f14,f2,f3,f5,f6",
                        },
                    )
                    resp.raise_for_status()
                    diff = ((resp.json().get("data") or {}).get("diff")) or []
                    for item in diff:
                        sym = item.get("f12")
                        price = _to_float(item.get("f2"))
                        if not sym or price is None:
                            continue
                        change_pct = _to_float(item.get("f3")) or 0.0
                        dollar = _to_float(item.get("f6"))
                        volume = _to_float(item.get("f5")) or 0.0
                        if dollar and price:
                            volume = dollar / price
                        out[sym] = _quote_row(
                            sym,
                            name=str(item.get("f14") or sym),
                            price=price,
                            change_pct=change_pct,
                            volume=volume,
                        )
                except Exception as exc:
                    logger.warning("Eastmoney batch failed: %s", exc)
    return out


def _last_good_path():
    from app.config import DATA_DIR

    return DATA_DIR / "heatmap_last_good.json"


def _load_last_good() -> dict[str, Any] | None:
    if _CACHE.get("last_good"):
        return _CACHE["last_good"]
    path = _last_good_path()
    try:
        if not path.exists():
            return None
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(data.get("_saved_at") or 0)
        if time.time() - saved_at > _LAST_GOOD_TTL:
            return None
        _CACHE["last_good"] = data
        return data
    except Exception as exc:
        logger.warning("load last_good failed: %s", exc)
        return None


def _save_last_good(data: dict[str, Any]) -> None:
    import json

    payload = {**data, "_saved_at": time.time()}
    _CACHE["last_good"] = payload
    try:
        _last_good_path().write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("save last_good failed: %s", exc)


def _is_quality_ok(quote_count: int, total: int) -> bool:
    if total <= 0:
        return False
    return quote_count >= max(20, int(total * _MIN_QUOTE_RATIO))


async def _fetch_quotes(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """多源合并：Yahoo spark（主）→ CNBC → 东财。优先稳定批量接口。"""
    sources_used: list[str] = []
    merged: dict[str, dict[str, Any]] = {}

    yahoo = await _fetch_yahoo(symbols)
    if yahoo:
        merged.update(yahoo)
        sources_used.append(f"Yahoo:{len(yahoo)}")
    if _is_quality_ok(len(merged), len(symbols)):
        return merged, "Yahoo"

    cnbc = await _fetch_cnbc(symbols)
    if cnbc:
        for k, v in cnbc.items():
            merged.setdefault(k, v)
        sources_used.append(f"CNBC:{len(cnbc)}")
    if _is_quality_ok(len(merged), len(symbols)):
        return merged, "+".join(sources_used) if len(sources_used) > 1 else "CNBC"

    east = await _fetch_eastmoney(symbols)
    if east:
        for k, v in east.items():
            merged.setdefault(k, v)
        sources_used.append(f"Eastmoney:{len(east)}")

    label = "+".join(sources_used) if sources_used else "none"
    if not _is_quality_ok(len(merged), len(symbols)):
        logger.error(
            "heatmap quotes low quality %s/%s via %s",
            len(merged),
            len(symbols),
            label,
        )
    return merged, label


async def _build_heatmap() -> dict[str, Any]:
    symbols: list[str] = []
    for sector in SECTORS:
        symbols.append(sector["etf"])
        symbols.extend(sym for sym, _ in sector["companies"])

    by_symbol, source = await _fetch_quotes(symbols)
    total = len(symbols)
    quote_count = len(by_symbol)

    if not _is_quality_ok(quote_count, total):
        stale = _load_last_good()
        if stale and _is_quality_ok(int(stale.get("quote_count") or 0), total):
            stale = dict(stale)
            stale.pop("_saved_at", None)
            stale["stale"] = True
            stale["note"] = (
                f"实时行情不完整（{source} 仅 {quote_count}/{total}），"
                f"已显示上次成功数据（{stale.get('source')} @ {stale.get('updated_at')}）。"
                "请稍后刷新。"
            )
            logger.warning("serving last_good heatmap due to low quality fetch")
            return stale

    sectors_out = []
    all_companies = []
    for sector in SECTORS:
        etf = by_symbol.get(sector["etf"])
        companies = []
        for sym, cn_name in sector["companies"]:
            row = by_symbol.get(sym)
            if not row:
                continue
            item = {
                **row,
                "cn_name": cn_name,
                "sector": sector["name"],
                "sector_key": sector["key"],
            }
            companies.append(item)
            all_companies.append(item)

        companies.sort(key=lambda x: x["flow_score"], reverse=True)
        sector_flow = sum(c["flow_score"] for c in companies)
        sector_dollar = sum(c["dollar_volume"] for c in companies)
        if etf:
            sector_change = etf["change_pct"]
        elif companies:
            sector_change = sum(c["change_pct"] for c in companies) / len(companies)
        else:
            sector_change = 0.0

        sectors_out.append(
            {
                "key": sector["key"],
                "name": sector["name"],
                "etf": sector["etf"],
                "change_pct": round(sector_change, 2),
                "dollar_volume": round(sector_dollar, 0),
                "flow_score": round(sector_flow, 4),
                "etf_quote": etf,
                "companies": companies,
            }
        )

    sectors_out.sort(key=lambda x: x["flow_score"], reverse=True)
    all_companies.sort(key=lambda x: x["flow_score"], reverse=True)

    if quote_count == 0:
        note = "行情拉取失败。已尝试 Yahoo/CNBC/东财仍无足够数据，请稍后刷新。"
    else:
        note = (
            f"数据源 {source}（{quote_count}/{total} 只）。"
            "本站% = 占监控样本（约11个主要板块+龙头股）的资金活跃度比重，"
            "非股价涨跌、非全市场资金占比；红流入绿流出。"
        )

    return {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "quote_count": quote_count,
        "quote_total": total,
        "stale": False,
        "note": note,
        "sectors": sectors_out,
        "top_inflow_sectors": [s for s in sectors_out if s["flow_score"] > 0][:5],
        "top_outflow_sectors": sorted(
            [s for s in sectors_out if s["flow_score"] < 0],
            key=lambda x: x["flow_score"],
        )[:5],
        "top_inflow_companies": [c for c in all_companies if c["flow_score"] > 0][:10],
        "top_outflow_companies": sorted(
            [c for c in all_companies if c["flow_score"] < 0],
            key=lambda x: x["flow_score"],
        )[:10],
    }


async def get_heatmap_data(force: bool = False) -> dict[str, Any]:
    now = time.time()
    cached = _CACHE.get("data")
    if (
        not force
        and cached is not None
        and now - _CACHE["ts"] < _CACHE_TTL
        and not cached.get("stale")
        and _is_quality_ok(
            int(cached.get("quote_count") or 0),
            int(cached.get("quote_total") or 72),
        )
    ):
        return cached

    data = await _build_heatmap()
    _CACHE["ts"] = now
    _CACHE["data"] = data
    if not data.get("stale") and _is_quality_ok(
        int(data.get("quote_count") or 0),
        int(data.get("quote_total") or 72),
    ):
        _save_last_good(data)
    return data


def _upsert_snapshot_rows(db, trade_date, rows: list[dict[str, Any]]) -> int:
    from app.database import HeatmapSnapshot

    db.query(HeatmapSnapshot).filter(HeatmapSnapshot.trade_date == trade_date).delete()
    for row in rows:
        db.add(HeatmapSnapshot(**row))
    db.commit()
    return len(rows)


async def save_daily_snapshot(trade_date=None, force: bool = False) -> dict[str, Any]:
    """拉取当日行情并写入每日快照（同一交易日可覆盖更新）。"""
    from app.database import SessionLocal, is_turso_stream_error, reset_engine
    from app.utils import is_us_trading_day, today_us

    trade_date = trade_date or today_us()
    if not force and not is_us_trading_day(trade_date):
        return {
            "success": True,
            "skipped": True,
            "reason": "非美股交易日",
            "trade_date": trade_date.isoformat(),
        }

    data = await get_heatmap_data(force=True)
    rows: list[dict[str, Any]] = []
    for sector in data["sectors"]:
        rows.append(
            {
                "trade_date": trade_date,
                "kind": "sector",
                "symbol": sector["etf"],
                "name": sector["name"],
                "cn_name": sector["name"],
                "sector_key": sector["key"],
                "sector_name": sector["name"],
                "price": (sector.get("etf_quote") or {}).get("price") or 0.0,
                "change_pct": sector["change_pct"],
                "volume": (sector.get("etf_quote") or {}).get("volume") or 0.0,
                "dollar_volume": sector["dollar_volume"],
                "flow_score": sector["flow_score"],
            }
        )
        for c in sector["companies"]:
            rows.append(
                {
                    "trade_date": trade_date,
                    "kind": "company",
                    "symbol": c["symbol"],
                    "name": c.get("name") or c["symbol"],
                    "cn_name": c.get("cn_name") or "",
                    "sector_key": sector["key"],
                    "sector_name": sector["name"],
                    "price": c["price"],
                    "change_pct": c["change_pct"],
                    "volume": c["volume"],
                    "dollar_volume": c["dollar_volume"],
                    "flow_score": c["flow_score"],
                }
            )

    db = SessionLocal()
    try:
        count = _upsert_snapshot_rows(db, trade_date, rows)
    except Exception as exc:
        if is_turso_stream_error(exc):
            reset_engine()
            db.close()
            db = SessionLocal()
            count = _upsert_snapshot_rows(db, trade_date, rows)
        else:
            raise
    finally:
        db.close()

    return {
        "success": True,
        "skipped": False,
        "trade_date": trade_date.isoformat(),
        "saved": count,
        "note": f"已保存 {trade_date} 快照；可用于周/月等周期统计",
    }


def _aggregate_period(records: list, kind: str) -> list[dict[str, Any]]:
    from collections import defaultdict

    grouped: dict[str, list] = defaultdict(list)
    for r in records:
        if r.kind != kind:
            continue
        grouped[r.symbol].append(r)

    out = []
    for symbol, items in grouped.items():
        items = sorted(items, key=lambda x: x.trade_date)
        first, last = items[0], items[-1]
        flow_sum = sum(i.flow_score for i in items)
        dollar_sum = sum(i.dollar_volume for i in items)
        if len(items) == 1:
            period_change_pct = last.change_pct
        elif first.price:
            period_change_pct = (last.price - first.price) / first.price * 100
        else:
            period_change_pct = sum(i.change_pct for i in items)

        out.append(
            {
                "symbol": symbol,
                "name": last.name,
                "cn_name": last.cn_name,
                "sector_key": last.sector_key,
                "sector_name": last.sector_name,
                "start_date": first.trade_date.isoformat(),
                "end_date": last.trade_date.isoformat(),
                "days": len(items),
                "start_price": round(first.price, 2),
                "end_price": round(last.price, 2),
                "period_change_pct": round(period_change_pct, 2),
                "flow_score_sum": round(flow_sum, 4),
                "dollar_volume_sum": round(dollar_sum, 0),
                "avg_daily_change_pct": round(
                    sum(i.change_pct for i in items) / len(items), 2
                ),
            }
        )

    out.sort(key=lambda x: x["flow_score_sum"], reverse=True)
    return out


def get_period_stats(period: str = "1w") -> dict[str, Any]:
    """按周期统计板块/公司资金变化（依赖每日快照）。

    快照交易日数不足该周期最低要求时，不返回排行数据，页面留空并说明原因。
    """
    from datetime import timedelta

    from sqlalchemy import desc

    from app.database import HeatmapSnapshot, SessionLocal, is_turso_stream_error, reset_engine
    from app.utils import today_us

    if period not in PERIODS:
        period = "1w"
    cfg = PERIODS[period]
    days = cfg["days"]
    min_snapshots = cfg["min_snapshots"]
    label = cfg["label"]
    since = today_us() - timedelta(days=days)

    def _query(db):
        latest = (
            db.query(HeatmapSnapshot.trade_date)
            .order_by(desc(HeatmapSnapshot.trade_date))
            .first()
        )
        records = (
            db.query(HeatmapSnapshot)
            .filter(HeatmapSnapshot.trade_date >= since)
            .order_by(HeatmapSnapshot.trade_date)
            .all()
        )
        return latest, records

    db = SessionLocal()
    try:
        latest, records = _query(db)
    except Exception as exc:
        if is_turso_stream_error(exc):
            reset_engine()
            db.close()
            db = SessionLocal()
            latest, records = _query(db)
        else:
            raise
    finally:
        db.close()

    dates = sorted({r.trade_date.isoformat() for r in records})
    snapshot_count = len(dates)
    latest_trade_date = latest[0].isoformat() if latest else None
    enough = snapshot_count >= min_snapshots

    empty = {
        "period": period,
        "label": label,
        "days": days,
        "min_snapshots": min_snapshots,
        "since": since.isoformat(),
        "snapshot_dates": dates,
        "snapshot_count": snapshot_count,
        "latest_trade_date": latest_trade_date,
        "sufficient": False,
        "sectors": [],
        "companies": [],
        "top_inflow_sectors": [],
        "top_outflow_sectors": [],
        "top_inflow_companies": [],
        "top_outflow_companies": [],
        "periods": {k: v["label"] for k, v in PERIODS.items()},
    }

    if snapshot_count == 0:
        empty["note"] = (
            f"「{label}」暂无快照数据。请先点击「存今日快照」，"
            "或等待美股收盘后（美东 16:30）自动保存；积累够天数后再查看该周期。"
        )
        return empty

    if not enough:
        empty["note"] = (
            f"「{label}」数据还不足：该周期至少需要 {min_snapshots} 个交易日快照，"
            f"当前只有 {snapshot_count} 天"
            + (f"（最新 {latest_trade_date}）" if latest_trade_date else "")
            + "。有数据的周期（如「每天」或「今日实时」）可正常查看；"
            "请继续每日保存快照，凑齐天数后再看周/月统计。"
        )
        return empty

    sectors = _aggregate_period(records, "sector")
    companies = _aggregate_period(records, "company")

    return {
        "period": period,
        "label": label,
        "days": days,
        "min_snapshots": min_snapshots,
        "since": since.isoformat(),
        "snapshot_dates": dates,
        "snapshot_count": snapshot_count,
        "latest_trade_date": latest_trade_date,
        "sufficient": True,
        "note": (
            "本站% = 占监控样本（主要板块+龙头股）的资金活跃度比重，非全市场。"
            "周期资金变化 = 区间内每日 flow_score 累计；悬停可看股价涨跌。"
        ),
        "sectors": sectors,
        "companies": companies,
        "top_inflow_sectors": [s for s in sectors if s["flow_score_sum"] > 0][:5],
        "top_outflow_sectors": sorted(
            [s for s in sectors if s["flow_score_sum"] < 0],
            key=lambda x: x["flow_score_sum"],
        )[:5],
        "top_inflow_companies": [c for c in companies if c["flow_score_sum"] > 0][:10],
        "top_outflow_companies": sorted(
            [c for c in companies if c["flow_score_sum"] < 0],
            key=lambda x: x["flow_score_sum"],
        )[:10],
        "periods": {k: v["label"] for k, v in PERIODS.items()},
    }
