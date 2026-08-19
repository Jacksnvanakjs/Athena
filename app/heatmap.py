"""美股板块/个股热力图数据。

主数据源：新浪财经美股批量行情（云端机房通常可用）；
补充：Yahoo spark / CNBC / 东财。
资金流入用「涨跌幅 × 成交额」代理指标；非 Level2 真实资金流。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CNBC_QUOTE = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
SINA_HQ = "https://hq.sinajs.cn/list={codes}"
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

# 成功样本过少时视为失败（避免「全 0」或「单板块 100%」）
_MIN_QUOTE_RATIO = 0.55
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL = 120  # 秒


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
            ("WFC", "富国银行"),
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

# 细粒度主题板块：优先用 ETF 实时涨跌幅（最准确），无 ETF 时回退到成分股等权平均。
# 主题命名对齐雪球/富途/市场魔法助手等主流平台。
THEMES: list[dict[str, Any]] = [
    {"key": "semi", "name": "半导体", "etf": "SOXX",
     "tickers": [("NVDA", "英伟达"), ("AMD", "AMD"), ("AVGO", "博通"), ("QCOM", "高通"), ("MU", "美光"), ("INTC", "英特尔"), ("MRVL", "Marvell"), ("TSM", "台积电"), ("ASML", "ASML"), ("ARM", "ARM")]},
    {"key": "semi_equipment", "name": "半导体设备",
     "tickers": [("ASML", "ASML"), ("AMAT", "应用材料"), ("LRCX", "拉姆研究"), ("KLAC", "KLA"), ("ONTO", "Onto Innovation")]},
    {"key": "semi_eda", "name": "EDA/IC设计",
     "tickers": [("SNPS", "Synopsys"), ("CDNS", "Cadence"), ("ANSS", "ANSYS"), ("MCHP", "微芯")]},
    {"key": "ai_compute", "name": "AI算力",
     "tickers": [("NVDA", "英伟达"), ("AMD", "AMD"), ("SMCI", "超微"), ("CRWV", "CoreWeave"), ("VRT", "Vertiv"), ("DELL", "戴尔"), ("IREN", "IREN"), ("APLD", "Applied Digital"), ("CORZ", "Core Scientific"), ("WULF", "TeraWulf")]},
    {"key": "cpo", "name": "CPO/光模块",
     "tickers": [("LITE", "Lumentum"), ("COHR", "Coherent"), ("CIEN", "Ciena"), ("FN", "Fabrinet"), ("AAOI", "Applied Opto"), ("GLW", "康宁"), ("MTSI", "MACOM"), ("AVGO", "博通")]},
    {"key": "storage", "name": "存储",
     "tickers": [("MU", "美光"), ("WDC", "西部数据"), ("STX", "希捷"), ("PSTG", "Pure Storage"), ("NTAP", "NetApp")]},
    {"key": "datacenter", "name": "数据中心/IDC", "etf": "SRVR",
     "tickers": [("EQIX", "Equinix"), ("DLR", "Digital Realty"), ("AMT", "美国电塔"), ("VRT", "Vertiv"), ("ANET", "Arista"), ("CRWV", "CoreWeave")]},
    {"key": "cloud_saas", "name": "云计算/SaaS", "etf": "SKYY",
     "tickers": [("MSFT", "微软"), ("AMZN", "亚马逊"), ("GOOGL", "谷歌"), ("CRM", "Salesforce"), ("NOW", "ServiceNow"), ("SNOW", "Snowflake"), ("ORCL", "甲骨文")]},
    {"key": "ai_software", "name": "AI应用/软件",
     "tickers": [("PLTR", "Palantir"), ("ADBE", "Adobe"), ("DDOG", "Datadog"), ("MDB", "MongoDB"), ("PATH", "UiPath"), ("AI", "C3.ai")]},
    {"key": "cybersecurity", "name": "网络安全", "etf": "HACK",
     "tickers": [("CRWD", "CrowdStrike"), ("PANW", "Palo Alto"), ("FTNT", "Fortinet"), ("ZS", "Zscaler"), ("OKTA", "Okta"), ("S", "SentinelOne")]},
    {"key": "network", "name": "网络设备",
     "tickers": [("ANET", "Arista"), ("CSCO", "思科"), ("JNPR", "Juniper"), ("FFIV", "F5"), ("NTNX", "Nutanix")]},
    {"key": "fintech", "name": "金融科技", "etf": "FINX",
     "tickers": [("V", "Visa"), ("MA", "万事达"), ("SQ", "Block"), ("PYPL", "PayPal"), ("AXP", "美国运通")]},
    {"key": "crypto", "name": "加密货币/区块链", "etf": "BITO",
     "tickers": [("COIN", "Coinbase"), ("RIOT", "Riot"), ("MARA", "Marathon"), ("HUT", "Hut 8"), ("CLSK", "CleanSpark")]},
    {"key": "ev", "name": "新能源车", "etf": "DRIV",
     "tickers": [("TSLA", "特斯拉"), ("NIO", "蔚来"), ("XPEV", "小鹏"), ("LI", "理想"), ("RIVN", "Rivian"), ("LCID", "Lucid")]},
    {"key": "ev_charging", "name": "充电桩",
     "tickers": [("EVGO", "EVgo"), ("BLNK", "Blink"), ("CHPT", "ChargePoint")]},
    {"key": "ev_battery", "name": "锂电池/电池材料", "etf": "LIT",
     "tickers": [("ALB", "Albemarle"), ("SQM", "SQM"), ("SLDP", "Solid Power"), ("ENVX", "Enovix"), ("QS", "QuantumScape")]},
    {"key": "autonomous", "name": "自动驾驶",
     "tickers": [("TSLA", "特斯拉"), ("NVDA", "英伟达"), ("QCOM", "高通"), ("MBLY", "Mobileye"), ("LAZR", "Luminar")]},
    {"key": "robotics", "name": "机器人", "etf": "ROBT",
     "tickers": [("ISRG", "直觉外科"), ("ROK", "Rockwell"), ("ABB", "ABB"), ("IRBT", "iRobot")]},
    {"key": "quantum", "name": "量子计算", "etf": "QTUM",
     "tickers": [("IONQ", "IonQ"), ("RGTI", "Rigetti"), ("QBTS", "D-Wave Quantum"), ("QUBT", "Quantum Computing")]},
    {"key": "biotech", "name": "生物科技", "etf": "IBB",
     "tickers": [("REGN", "Regeneron"), ("BIIB", "Biogen"), ("MRNA", "Moderna"), ("VRTX", "Vertex"), ("GILD", "吉利德")]},
    {"key": "pharma", "name": "创新药/大药企", "etf": "XLV",
     "tickers": [("LLY", "礼来"), ("ABBV", "艾伯维"), ("MRK", "默沙东"), ("JNJ", "强生"), ("PFE", "辉瑞"), ("NVO", "诺和诺德")]},
    {"key": "med_devices", "name": "医疗器械", "etf": "IHI",
     "tickers": [("ISRG", "直觉外科"), ("MDT", "美敦力"), ("SYK", "史赛克"), ("BSX", "波士顿科学"), ("EW", "Edwards")]},
    {"key": "clean_energy", "name": "清洁能源", "etf": "ICLN",
     "tickers": [("ENPH", "Enphase"), ("FSLR", "First Solar"), ("SEDG", "SolarEdge"), ("NEE", "新纪元能源")]},
    {"key": "solar", "name": "光伏", "etf": "TAN",
     "tickers": [("FSLR", "First Solar"), ("ENPH", "Enphase"), ("SEDG", "SolarEdge"), ("RUN", "Sunrun"), ("NOVA", "Sunnova")]},
    {"key": "nuclear", "name": "核能/铀", "etf": "URNM",
     "tickers": [("CCJ", "Cameco"), ("UEC", "Uranium Energy"), ("OKLO", "Oklo"), ("NNE", "Nano Nuclear")]},
    {"key": "dc_power", "name": "电力/独立发电",
     "tickers": [("VST", "Vistra"), ("CEG", "Constellation"), ("NRG", "NRG"), ("GEV", "GE Vernova"), ("OKLO", "Oklo")]},
    {"key": "grid", "name": "电网/电气设备",
     "tickers": [("ETN", "Eaton"), ("GEV", "GE Vernova"), ("PWR", "Quanta"), ("EMR", "艾默生"), ("GNRC", "Generac")]},
    {"key": "oil_gas", "name": "油气", "etf": "XLE",
     "tickers": [("XOM", "埃克森美孚"), ("CVX", "雪佛龙"), ("COP", "康菲"), ("SLB", "斯伦贝谢"), ("EOG", "EOG")]},
    {"key": "gold", "name": "黄金", "etf": "GDX",
     "tickers": [("NEM", "纽蒙特"), ("GOLD", "巴里克"), ("FNV", "Franco-Nevada"), ("WPM", "惠顿贵金属")]},
    {"key": "rare_earth", "name": "稀土/战略金属", "etf": "REMX",
     "tickers": [("MP", "MP Materials"), ("ALB", "Albemarle"), ("FCX", "自由港")]},
    {"key": "defense", "name": "国防军工", "etf": "ITA",
     "tickers": [("LMT", "洛马"), ("NOC", "诺斯罗普"), ("RTX", "雷神"), ("GD", "通用动力"), ("LHX", "L3Harris")]},
    {"key": "space", "name": "航天/卫星",
     "tickers": [("RKLB", "Rocket Lab"), ("ASTS", "AST SpaceMobile"), ("PL", "Planet Labs"), ("LHX", "L3Harris")]},
    {"key": "telecom_5g", "name": "5G/通信",
     "tickers": [("TMUS", "T-Mobile"), ("VZ", "Verizon"), ("T", "AT&T"), ("ERIC", "爱立信"), ("NOK", "诺基亚")]},
    {"key": "gaming", "name": "游戏", "etf": "HERO",
     "tickers": [("EA", "艺电"), ("TTWO", "Take-Two"), ("RBLX", "Roblox"), ("U", "Unity")]},
    {"key": "streaming", "name": "流媒体/传媒",
     "tickers": [("NFLX", "Netflix"), ("DIS", "迪士尼"), ("CMCSA", "康卡斯特"), ("WBD", "华纳兄弟")]},
    {"key": "3d_printing", "name": "3D打印", "etf": "PRNT",
     "tickers": [("DDD", "3D Systems"), ("SSYS", "Stratasys")]},
    {"key": "cloud_collab", "name": "远程办公/协作",
     "tickers": [("ZM", "Zoom"), ("TEAM", "Atlassian"), ("NET", "Cloudflare"), ("DOCN", "DigitalOcean")]},
    {"key": "rideshare", "name": "共享出行/外卖",
     "tickers": [("DASH", "DoorDash"), ("UBER", "Uber"), ("LYFT", "Lyft")]},
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


def _sina_code(symbol: str) -> str:
    return "gb_" + symbol.lower().replace("-", ".")


async def _fetch_sina(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """新浪财经美股批量行情（hq.sinajs.cn），云端机房通常不被拦。"""
    out: dict[str, dict[str, Any]] = {}
    code_to_sym = {_sina_code(s): s for s in symbols}
    headers = {
        **HEADERS,
        "Referer": "https://finance.sina.com.cn/",
        "Accept": "*/*",
    }
    chunk_size = 40
    codes = list(code_to_sym.keys())
    async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=True) as client:
        for i in range(0, len(codes), chunk_size):
            chunk = codes[i : i + chunk_size]
            try:
                resp = await client.get(SINA_HQ.format(codes=",".join(chunk)))
                resp.raise_for_status()
                raw = resp.content
                try:
                    text = raw.decode("gb18030")
                except UnicodeDecodeError:
                    text = raw.decode("utf-8", errors="ignore")
                for code, sym in ((c, code_to_sym[c]) for c in chunk):
                    match = re.search(
                        rf'hq_str_{re.escape(code)}="([^"]*)"',
                        text,
                    )
                    if not match or not match.group(1):
                        continue
                    parts = match.group(1).split(",")
                    if len(parts) < 11:
                        continue
                    price = _to_float(parts[1])
                    change_pct = _to_float(parts[2])
                    volume = _to_float(parts[10])
                    # 盘前/盘后: 字段[21]=延时价, [26]=昨收价
                    # 用延时价相对昨收算总涨跌幅，与"市场魔法助手"等一致
                    if len(parts) >= 27:
                        ext_price = _to_float(parts[21])
                        prev_close = _to_float(parts[26])
                        if ext_price and ext_price > 0 and prev_close and prev_close > 0:
                            price = ext_price
                            change_pct = round((ext_price - prev_close) / prev_close * 100, 2)
                    if price is None or price <= 0:
                        continue
                    out[sym] = _quote_row(
                        sym,
                        name=parts[0] or sym,
                        price=price,
                        change_pct=change_pct or 0.0,
                        volume=volume or 0.0,
                    )
            except Exception as exc:
                logger.warning("Sina batch failed: %s", exc)
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


def _is_quality_ok(quote_count: int, total: int) -> bool:
    if total <= 0:
        return False
    return quote_count >= max(20, int(total * _MIN_QUOTE_RATIO))


async def _fetch_quotes(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """多源合并：新浪（主）→ Yahoo → CNBC → 东财。"""
    sources_used: list[str] = []
    merged: dict[str, dict[str, Any]] = {}

    sina = await _fetch_sina(symbols)
    if sina:
        merged.update(sina)
        sources_used.append(f"Sina:{len(sina)}")
    if _is_quality_ok(len(merged), len(symbols)):
        return merged, "Sina"

    yahoo = await _fetch_yahoo(symbols)
    if yahoo:
        for k, v in yahoo.items():
            merged.setdefault(k, v)
        sources_used.append(f"Yahoo:{len(yahoo)}")
    if _is_quality_ok(len(merged), len(symbols)):
        return merged, "+".join(sources_used) if len(sources_used) > 1 else "Yahoo"

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


def _heatmap_failure(source: str, quote_count: int, total: int) -> dict[str, Any]:
    note = (
        f"行情拉取失败（{source} 仅 {quote_count}/{total}）。"
        "已尝试 新浪 / Yahoo / CNBC / 东财，请稍后点击刷新。"
    )
    return {
        "success": False,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "quote_count": quote_count,
        "quote_total": total,
        "note": note,
        "sectors": [],
        "top_inflow_sectors": [],
        "top_outflow_sectors": [],
        "top_inflow_companies": [],
        "top_outflow_companies": [],
        "themes": [],
    }


def _sector_symbols() -> list[str]:
    symbols: list[str] = []
    for sector in SECTORS:
        symbols.append(sector["etf"])
        symbols.extend(sym for sym, _ in sector["companies"])
    return list(dict.fromkeys(symbols))


def _theme_symbols() -> list[str]:
    symbols: list[str] = []
    for theme in THEMES:
        etf = theme.get("etf")
        if etf:
            symbols.append(etf)
        symbols.extend(sym for sym, _ in theme["tickers"])
    return list(dict.fromkeys(symbols))


def _build_themes(by_symbol: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    themes_out: list[dict[str, Any]] = []
    for theme in THEMES:
        companies: list[dict[str, Any]] = []
        for sym, cn_name in theme["tickers"]:
            row = by_symbol.get(sym)
            if not row:
                continue
            companies.append({**row, "cn_name": cn_name})

        etf = theme.get("etf")
        etf_row = by_symbol.get(etf) if etf else None
        total = len(theme["tickers"])
        quoted = len(companies)

        if etf_row:
            change_pct = etf_row["change_pct"]
        elif companies:
            change_pct = sum(c["change_pct"] for c in companies) / len(companies)
        else:
            change_pct = 0.0

        themes_out.append(
            {
                "key": theme["key"],
                "name": theme["name"],
                "etf": etf,
                "change_pct": round(change_pct, 2),
                "quote_count": quoted,
                "quote_total": total,
                "etf_quote": etf_row,
                "companies": sorted(companies, key=lambda x: x["change_pct"], reverse=True),
            }
        )

    themes_out.sort(key=lambda x: x["change_pct"], reverse=True)
    return themes_out


async def _build_heatmap() -> dict[str, Any]:
    sector_syms = _sector_symbols()
    theme_syms = _theme_symbols()
    symbols = list(dict.fromkeys(sector_syms + theme_syms))

    by_symbol, source = await _fetch_quotes(symbols)
    sector_hits = sum(1 for s in sector_syms if s in by_symbol)
    total_sector = len(sector_syms)
    quote_count = len(by_symbol)

    if not _is_quality_ok(sector_hits, total_sector):
        logger.error(
            "heatmap fetch failed sector %s/%s total_quotes %s via %s",
            sector_hits,
            total_sector,
            quote_count,
            source,
        )
        failure = _heatmap_failure(source, sector_hits, total_sector)
        failure["themes"] = _build_themes(by_symbol) if by_symbol else []
        failure["quote_count"] = quote_count
        failure["quote_total"] = len(symbols)
        return failure

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

    note = (
        f"数据源 {source}（板块 {sector_hits}/{total_sector}，合计 {quote_count}/{len(symbols)} 只）。"
        "本站% = 占监控样本（约11个主要板块+龙头股）的资金活跃度比重，"
        "非股价涨跌、非全市场资金占比；红流入绿流出。"
        "主题涨跌幅 = 有代表性 ETF 的主题优先使用 ETF 实时价格，其余为成分股等权平均。"
    )

    return {
        "success": True,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": source,
        "quote_count": quote_count,
        "quote_total": len(symbols),
        "note": note,
        "themes": _build_themes(by_symbol),
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
        and cached.get("success")
        and now - _CACHE["ts"] < _CACHE_TTL
        and _is_quality_ok(
            int(cached.get("quote_count") or 0),
            int(cached.get("quote_total") or 72),
        )
    ):
        return cached

    data = await _build_heatmap()
    _CACHE["ts"] = now
    if data.get("success"):
        _CACHE["data"] = data
    else:
        _CACHE["data"] = None
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
