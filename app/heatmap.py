"""美股板块/个股热力图数据。

行情源（按序尝试，前者成功则后者仅补缺）：
  1. TickDB（需环境变量 TICKDB_API_KEY，美股格式 SYMBOL.US）
  2. Yahoo Finance（含盘前/盘后价量分离；本地可设 HEATMAP_SKIP_YAHOO=1 跳过）
  3. AKShare：东财 ulist → 新浪 stock_us_daily 逐只 → 东财全表
  4. Tushare us_daily（需 TUSHARE_TOKEN，日频收盘非实时）
资金流入 = 涨跌幅 × 成交额 / 10亿；排行占比为样本内比重。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.utils import now_beijing

logger = logging.getLogger(__name__)

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
TICKDB_TICKER = "https://api.tickdb.ai/v1/market/ticker"
TICKDB_BATCH_SIZE = 25
TICKDB_BATCH_PAUSE = 1.5  # 秒，避免免费额度 429

# 成功样本过少时视为失败（避免「全 0」或「单板块 100%」）
_MIN_QUOTE_RATIO = 0.55
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL = 180  # 秒（3 分钟，减轻 Yahoo 429 与兜底源压力）
_SINA_SPOT_CACHE: dict[str, Any] = {"ts": 0.0, "df": None}
_SINA_SPOT_TTL = 180
_SINA_SPOT_FETCH_TIMEOUT = 600  # 新浪全表约 900 页，本地网络需 7–10 分钟
_BUILD_LOCK: asyncio.Lock | None = None


def _heatmap_build_lock() -> asyncio.Lock:
    global _BUILD_LOCK
    if _BUILD_LOCK is None:
        _BUILD_LOCK = asyncio.Lock()
    return _BUILD_LOCK


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


# 收盘快照入库时刻说明（PERIODS「每日」等均指该时刻的美东交易日收盘数据）
SNAPSHOT_TIME_DESC = (
    "美东每个交易日 16:30 自动入库"
    "（北京次日凌晨 04:30 冬令时 / 05:30 夏令时）"
)


PERIODS: dict[str, dict[str, Any]] = {
    # min_snapshots: 至少需要多少个交易日快照才展示该周期，否则留空
    # desc: 该周期的时间口径（展示在页面说明里）
    # rank_label: 右侧排行标题前缀（如「当日资金流入最多」）
    "1d": {
        "days": 1,
        "label": "每日",
        "rank_label": "当日",
        "min_snapshots": 1,
        "desc": (
            "最近一个美东交易日的收盘快照。"
            f"入库时间：{SNAPSHOT_TIME_DESC}。"
            "涨跌幅 = 相对上一美股交易日收盘价；成交额 = 该日全日成交量；非实时。"
        ),
    },
    "1w": {
        "days": 7,
        "label": "每周",
        "rank_label": "本周",
        "min_snapshots": 5,
        "desc": (
            "累计最近约 7 个自然日内、各美东交易日的收盘快照"
            "（至少需 5 个交易日；缺日则等补齐）。"
            f"每条快照均为{SNAPSHOT_TIME_DESC}。"
        ),
    },
    "15d": {
        "days": 15,
        "label": "每半月",
        "rank_label": "半月",
        "min_snapshots": 10,
        "desc": (
            "累计最近约 15 个自然日内、各美东交易日的收盘快照（至少需 10 个交易日）。"
            f"每条快照均为{SNAPSHOT_TIME_DESC}。"
        ),
    },
    "1m": {
        "days": 30,
        "label": "每月",
        "rank_label": "本月",
        "min_snapshots": 20,
        "desc": (
            "累计最近约 30 个自然日内、各美东交易日的收盘快照（至少需 20 个交易日）。"
            f"每条快照均为{SNAPSHOT_TIME_DESC}。"
        ),
    },
    "2m": {
        "days": 60,
        "label": "每2个月",
        "rank_label": "近两月",
        "min_snapshots": 40,
        "desc": (
            "累计最近约 60 个自然日内、各美东交易日的收盘快照（至少需 40 个交易日）。"
            f"每条快照均为{SNAPSHOT_TIME_DESC}。"
        ),
    },
    "3m": {
        "days": 90,
        "label": "每3个月",
        "rank_label": "近三月",
        "min_snapshots": 55,
        "desc": (
            "累计最近约 90 个自然日内、各美东交易日的收盘快照（至少需 55 个交易日）。"
            f"每条快照均为{SNAPSHOT_TIME_DESC}。"
        ),
    },
}


def _periods_for_api() -> dict[str, dict[str, str]]:
    return {
        k: {
            "label": v["label"],
            "rank_label": v.get("rank_label", v["label"]),
            "desc": v.get("desc", ""),
        }
        for k, v in PERIODS.items()
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
    quote_time: str | None = None,
    quote_time_et: str | None = None,
) -> dict[str, Any]:
    dollar_volume = price * volume
    row = {
        "symbol": symbol,
        "name": name or symbol,
        "price": round(price, 2),
        "change_pct": round(change_pct, 2),
        "volume": int(volume),
        "dollar_volume": round(dollar_volume, 0),
        "flow_score": round(change_pct * dollar_volume / 1e9, 4),
        "market_cap": market_cap,
    }
    if quote_time:
        row["quote_time"] = quote_time
    if quote_time_et:
        row["quote_time_et"] = quote_time_et
    return row


_US_TZ = ZoneInfo("America/New_York")
_BJ_TZ = ZoneInfo("Asia/Shanghai")


def _parse_sina_et_time(text: str) -> datetime | None:
    """解析新浪美东时间，如 'Aug 19 07:59PM EDT'。"""
    text = (text or "").strip()
    if not text:
        return None
    m = re.match(
        r"^([A-Za-z]{3}\s+\d{1,2}\s+\d{1,2}:\d{2}\s*(?:AM|PM))\s*(?:EDT|EST|ET)?$",
        text,
        re.I,
    )
    if not m:
        return None
    raw = re.sub(r"\s+", " ", m.group(1).strip())
    now_et = datetime.now(_US_TZ)
    for year in (now_et.year, now_et.year - 1):
        try:
            naive = datetime.strptime(f"{raw} {year}", "%b %d %I:%M%p %Y")
        except ValueError:
            continue
        aware = naive.replace(tzinfo=_US_TZ)
        # 远离当前时刻的年份视为错误，换一年再试
        if abs((aware - now_et).total_seconds()) <= 180 * 24 * 3600:
            return aware
    return None


def _us_market_session(now_et: datetime | None = None) -> dict[str, str]:
    """美股交易时段（按美东时间）。"""
    now_et = now_et or datetime.now(_US_TZ)
    minutes = now_et.hour * 60 + now_et.minute
    weekday = now_et.weekday()  # 0=Mon
    if weekday >= 5:
        return {
            "session": "closed",
            "session_label": "周末休市",
            "data_freshness": "休市中，显示最近一个交易日收盘/盘后价",
            "change_pct_basis": "涨跌幅为最近一个交易日收盘价",
        }
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return {
            "session": "pre",
            "session_label": "盘前交易",
            "data_freshness": "盘前实时（Yahoo Finance）",
            "change_pct_basis": "涨跌幅相对上一交易日收盘价（盘前）",
        }
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return {
            "session": "regular",
            "session_label": "盘中交易",
            "data_freshness": "盘中实时（Yahoo Finance）",
            "change_pct_basis": "涨跌幅相对昨收（盘中）",
        }
    if 16 * 60 <= minutes < 20 * 60:
        return {
            "session": "post",
            "session_label": "盘后交易",
            "data_freshness": "盘后实时（Yahoo Finance）",
            "change_pct_basis": "涨跌幅相对昨收（盘后）",
        }
    return {
        "session": "overnight",
        "session_label": "隔夜休市",
        "data_freshness": "隔夜休市，价格停在昨盘后；开盘前（美东04:00起）才会继续变动",
        "change_pct_basis": "涨跌幅停在昨盘后",
    }


def _parse_sina_row(parts: list[str], sym: str) -> dict[str, Any] | None:
    """解析新浪 hq 字段；盘中勿用 [21] 盘前价覆盖 [1] 最新价。"""
    if len(parts) < 11:
        return None
    price = _to_float(parts[1])
    change_pct = _to_float(parts[2]) or 0.0
    volume = _to_float(parts[10]) or 0.0
    prev_close = _to_float(parts[26]) if len(parts) > 26 else None
    ext_price = _to_float(parts[21]) if len(parts) > 21 else None
    et_raw = parts[24].strip() if len(parts) > 24 else ""
    et_dt = _parse_sina_et_time(et_raw)
    ref_et = et_dt.astimezone(_US_TZ) if et_dt else datetime.now(_US_TZ)
    session = _us_market_session(ref_et).get("session", "closed")

    if session == "regular":
        # 盘中：字段 [1] 为最新成交，[21] 可能仍是滞后盘前价
        if price and prev_close and prev_close > 0:
            change_pct = round((price - prev_close) / prev_close * 100, 2)
    elif session in ("pre", "post"):
        if ext_price and ext_price > 0 and prev_close and prev_close > 0:
            price = ext_price
            change_pct = round((ext_price - prev_close) / prev_close * 100, 2)
    elif ext_price and ext_price > 0 and prev_close and prev_close > 0:
        price = ext_price
        change_pct = round((ext_price - prev_close) / prev_close * 100, 2)
    elif price and prev_close and prev_close > 0:
        change_pct = round((price - prev_close) / prev_close * 100, 2)

    if price is None or price <= 0:
        return None
    quote_time = (
        et_dt.astimezone(_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S") if et_dt else None
    )
    return _quote_row(
        sym,
        name=parts[0] or sym,
        price=price,
        change_pct=change_pct,
        volume=volume,
        quote_time=quote_time,
        quote_time_et=et_raw or None,
    )


def _snapshot_time_hint() -> str:
    """美东 16:30 收盘存快照对应的北京时间。"""
    now_et = datetime.now(_US_TZ)
    close_et = now_et.replace(hour=16, minute=30, second=0, microsecond=0)
    close_bj = close_et.astimezone(_BJ_TZ)
    return (
        f"北京 {close_bj.strftime('%H:%M')} · 美东 16:30"
    )


def _response_timestamps(by_symbol: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """页面时间以北京时间为主，同时附带美东时间。"""
    from app.utils import today_us

    now_et = datetime.now(_US_TZ)
    now_bj = now_et.astimezone(_BJ_TZ)
    session = _us_market_session(now_et)
    out: dict[str, str] = {
        "updated_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_bj": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_label": "北京时间",
        "market_time_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "market_time_bj": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "trade_date_us": today_us().isoformat(),
        "snapshot_hint": _snapshot_time_hint(),
        **session,
    }
    if not by_symbol:
        return out

    quote_times = sorted(
        {r["quote_time"] for r in by_symbol.values() if r.get("quote_time")}
    )
    if quote_times:
        out["quote_time"] = quote_times[-1]
        out["quote_time_label"] = "北京时间"
    quote_times_et = sorted(
        {r["quote_time_et"] for r in by_symbol.values() if r.get("quote_time_et")}
    )
    if quote_times_et:
        out["quote_time_et"] = quote_times_et[-1]
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


def _sum_yahoo_session_volume(result: dict[str, Any], *, state: str) -> float:
    """从 1 分钟 K 汇总当前时段成交量（避免盘前误用昨安全天量）。"""
    meta = result.get("meta") or {}
    reg_open = meta.get("regularMarketTime")
    ts = result.get("timestamp") or []
    vols = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("volume") or []
    total = 0.0
    for t, v in zip(ts, vols):
        if not v:
            continue
        tv = float(v)
        if state == "PRE":
            if reg_open and t >= reg_open:
                continue
        elif state in ("POST", "POSTPOST"):
            if reg_open and t < reg_open:
                continue
        total += tv
    return total


def _parse_yahoo_meta(
    meta: dict[str, Any],
    result: dict[str, Any] | None,
    symbol: str,
) -> dict[str, Any] | None:
    """按 marketState 取价/涨跌/量，涨跌幅均相对 previousClose。"""
    state = str(meta.get("marketState") or "").upper()
    prev = _to_float(meta.get("previousClose")) or _to_float(
        meta.get("chartPreviousClose")
    )
    price: float | None = None
    change_pct: float | None = None
    volume = 0.0

    if state == "PRE":
        price = _to_float(meta.get("preMarketPrice"))
        change_pct = _to_float(meta.get("preMarketChangePercent"))
        volume = _to_float(meta.get("preMarketVolume")) or 0.0
        if result and volume <= 0:
            volume = _sum_yahoo_session_volume(result, state="PRE")
    elif state in ("POST", "POSTPOST"):
        price = _to_float(meta.get("postMarketPrice"))
        change_pct = _to_float(meta.get("postMarketChangePercent"))
        volume = _to_float(meta.get("postMarketVolume")) or 0.0
        if result and volume <= 0:
            volume = _sum_yahoo_session_volume(result, state=state)
    else:
        price = _to_float(meta.get("regularMarketPrice"))
        change_pct = _to_float(meta.get("regularMarketChangePercent"))
        volume = _to_float(meta.get("regularMarketVolume")) or 0.0
        if result and volume <= 0 and state == "REGULAR":
            volume = _sum_yahoo_session_volume(result, state="REGULAR")

    if price is None and result:
        closes = [
            c
            for c in (
                ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close")
                or []
            )
            if c is not None
        ]
        if closes:
            price = float(closes[-1])
    if price is None:
        return None
    if change_pct is None:
        if prev and prev > 0:
            change_pct = round((price - prev) / prev * 100, 2)
        else:
            change_pct = 0.0

    quote_time_et = None
    quote_time = None
    ts_key = {
        "PRE": "preMarketTime",
        "POST": "postMarketTime",
        "POSTPOST": "postMarketTime",
    }.get(state, "regularMarketTime")
    raw_ts = meta.get(ts_key) or meta.get("regularMarketTime")
    if raw_ts:
        try:
            dt = datetime.fromtimestamp(int(raw_ts), tz=_US_TZ)
            quote_time_et = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            quote_time = dt.astimezone(_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            pass

    return _quote_row(
        symbol,
        name=meta.get("longName") or meta.get("shortName") or symbol,
        price=price,
        change_pct=change_pct,
        volume=volume,
        quote_time=quote_time,
        quote_time_et=quote_time_et,
    )


def _parse_yahoo_chart_result(result: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    meta = result.get("meta") or {}
    sym = _from_yahoo_symbol(
        str(meta.get("symbol") or symbol),
        {symbol, _yahoo_symbol(symbol), symbol.replace(".", "-")},
    )
    return _parse_yahoo_meta(meta, result, sym)


def _parse_yahoo_spark_item(
    item: dict[str, Any], wanted: set[str]
) -> tuple[str, dict[str, Any]] | None:
    yahoo_sym = item.get("symbol") or ""
    resp = (item.get("response") or [{}])[0] or {}
    sym = _from_yahoo_symbol(str(yahoo_sym), wanted)
    row = _parse_yahoo_meta(resp.get("meta") or {}, resp, sym)
    if not row:
        return None
    return sym, row


async def _fetch_yahoo_spark(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Yahoo spark 批量报价：一次最多约 20 只。"""
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
                        "range": "1d",
                        "interval": "1m",
                    },
                )
                if resp.status_code == 429:
                    logger.warning("Yahoo spark rate limited; skip remaining batches")
                    break
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
    client: httpx.AsyncClient, symbol: str, delay: float = 0.0
) -> tuple[str, dict[str, Any] | None]:
    import asyncio

    if delay:
        await asyncio.sleep(delay)
    ysym = _yahoo_symbol(symbol)
    params = {"interval": "1m", "range": "1d", "includePrePost": "true"}
    for attempt in range(4):
        try:
            resp = await client.get(YAHOO_CHART.format(symbol=ysym), params=params)
            if resp.status_code == 429:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return symbol, None
            result = ((resp.json().get("chart") or {}).get("result")) or []
            if not result:
                return symbol, None
            row = _parse_yahoo_chart_result(result[0], symbol)
            return symbol, row
        except Exception as exc:
            logger.debug("Yahoo quote failed %s: %s", symbol, exc)
            await asyncio.sleep(0.8)
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
    sem = asyncio.Semaphore(2)
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:

        async def one(idx: int, sym: str):
            async with sem:
                return await _fetch_yahoo_one(client, sym, delay=idx * 0.35)

        results = await asyncio.gather(*[one(i, s) for i, s in enumerate(missing)])
    for sym, row in results:
        if row:
            out[sym] = row
    return out


async def _fetch_yahoo(symbols: list[str]) -> dict[str, dict[str, Any]]:
    out = await _fetch_yahoo_spark(symbols)
    missing = [s for s in symbols if s not in out]
    if missing and (len(symbols) <= 40 or len(missing) <= 40):
        out.update(await _fetch_yahoo_chart_missing(symbols, out))
    elif missing:
        logger.warning(
            "Yahoo partial %s/%s; skip chart for %s missing (fallback sources)",
            len(out),
            len(symbols),
            len(missing),
        )
    return out


def _akshare_us_spot_table():
    """同步拉 AKShare 东方财富美股现货表。"""
    import os

    import akshare as ak

    saved = {
        k: os.environ.pop(k, None)
        for k in list(os.environ)
        if "proxy" in k.lower()
    }
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    try:
        return ak.stock_us_spot_em()
    finally:
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _parse_akshare_row(row: Any, symbol: str) -> dict[str, Any] | None:
    price = _to_float(row.get("最新价"))
    if price is None:
        return None
    change_pct = _to_float(row.get("涨跌幅")) or 0.0
    volume = _to_float(row.get("成交量")) or 0.0
    dollar = _to_float(row.get("成交额")) or 0.0
    if volume <= 0 and dollar and price:
        volume = dollar / price
    return _quote_row(
        symbol,
        name=str(row.get("名称") or symbol),
        price=price,
        change_pct=change_pct,
        volume=volume,
    )


async def _fetch_akshare_em_direct(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """AKShare 同源的东财 ulist 接口（库调用失败时的兜底）。"""
    out: dict[str, dict[str, Any]] = {}
    headers = {**HEADERS, "Referer": "https://quote.eastmoney.com/"}
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    markets = (105, 106, 107)
    fail_streak = 0
    async with httpx.AsyncClient(headers=headers, timeout=12, follow_redirects=True) as client:
        for mkt in markets:
            missing = [s for s in symbols if s not in out]
            if not missing:
                break
            for i in range(0, len(missing), 20):
                chunk = missing[i : i + 20]
                secids = ",".join(f"{mkt}.{s}" for s in chunk)
                try:
                    resp = await client.get(
                        url,
                        params={
                            "fltt": "2",
                            "secids": secids,
                            "fields": "f12,f14,f2,f3,f5,f6",
                        },
                    )
                    resp.raise_for_status()
                    diff = ((resp.json().get("data") or {}).get("diff")) or []
                    if not diff:
                        fail_streak += 1
                    else:
                        fail_streak = 0
                    for item in diff:
                        sym = str(item.get("f12") or "").upper()
                        if sym not in symbols:
                            continue
                        price = _to_float(item.get("f2"))
                        if price is None:
                            continue
                        change_pct = _to_float(item.get("f3")) or 0.0
                        dollar = _to_float(item.get("f6"))
                        volume = _to_float(item.get("f5")) or 0.0
                        if volume <= 0 and dollar and price:
                            volume = dollar / price
                        out[sym] = _quote_row(
                            sym,
                            name=str(item.get("f14") or sym),
                            price=price,
                            change_pct=change_pct,
                            volume=volume,
                        )
                except Exception as exc:
                    fail_streak += 1
                    logger.warning("AKShare EM direct batch failed: %s", exc)
                if fail_streak >= 3:
                    logger.warning(
                        "AKShare EM direct abort after %s consecutive failures (%s/%s)",
                        fail_streak,
                        len(out),
                        len(symbols),
                    )
                    return out
    return out


def _akshare_sina_spot_table():
    """AKShare 新浪美股现货全表（较慢，作兜底）。"""
    import os

    import akshare as ak

    saved = {k: os.environ.pop(k, None) for k in list(os.environ) if "proxy" in k.lower()}
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    try:
        return ak.stock_us_spot()
    finally:
        os.environ.pop("NO_PROXY", None)
        os.environ.pop("no_proxy", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def _parse_akshare_sina_row(row: Any, symbol: str) -> dict[str, Any] | None:
    price = _to_float(row.get("price"))
    if price is None:
        return None
    change_pct = _to_float(row.get("chg")) or 0.0
    volume = _to_float(row.get("volume")) or 0.0
    return _quote_row(
        symbol,
        name=str(row.get("name") or row.get("cname") or symbol),
        price=price,
        change_pct=change_pct,
        volume=volume,
    )


def _akshare_sina_daily_one(symbol: str) -> dict[str, Any] | None:
    """AKShare 新浪 stock_us_daily 单标的（日线收盘，本地网络较稳）。"""
    import akshare as ak

    try:
        df = ak.stock_us_daily(symbol=symbol, adjust="")
    except Exception as exc:
        logger.debug("AKShare sina daily %s failed: %s", symbol, exc)
        return None
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    price = _to_float(last.get("close"))
    if price is None:
        return None
    volume = _to_float(last.get("volume")) or 0.0
    change_pct = 0.0
    if len(df) >= 2:
        prev = _to_float(df.iloc[-2].get("close"))
        if prev and prev > 0:
            change_pct = round((price - prev) / prev * 100, 2)
    return _quote_row(symbol, name=symbol, price=price, change_pct=change_pct, volume=volume)


def _fetch_akshare_sina_daily_sync(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """同步逐只拉新浪日线（AKShare 不宜多线程，单线程约 40s/231 只）。"""
    out: dict[str, dict[str, Any]] = {}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        if sym in out:
            continue
        parsed = _akshare_sina_daily_one(sym)
        if parsed:
            out[sym] = parsed
        if total >= 20 and (i + 1) % 25 == 0:
            logger.info("AKShare sina daily progress %s/%s", i + 1, total)
    return out


async def _fetch_akshare_sina_daily(symbols: list[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_akshare_sina_daily_sync, symbols),
            timeout=180,
        )
    except Exception as exc:
        logger.warning("AKShare sina daily batch failed: %s", exc)
        return {}


async def _fetch_akshare_sina(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """AKShare stock_us_spot（新浪源）；全表缓存 120s 避免重复拉 900+ 页。"""
    import asyncio
    import time

    wanted = set(symbols)
    if not wanted:
        return {}
    now = time.time()
    df = None
    if (
        _SINA_SPOT_CACHE.get("df") is not None
        and now - float(_SINA_SPOT_CACHE.get("ts") or 0) < _SINA_SPOT_TTL
    ):
        df = _SINA_SPOT_CACHE["df"]
    if df is None:
        try:
            df = await asyncio.wait_for(
                asyncio.to_thread(_akshare_sina_spot_table),
                timeout=_SINA_SPOT_FETCH_TIMEOUT,
            )
            _SINA_SPOT_CACHE["df"] = df
            _SINA_SPOT_CACHE["ts"] = now
        except Exception as exc:
            logger.warning("AKShare sina spot failed: %s", exc)
            return {}
    out: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        sym = str(row.get("symbol") or "").strip().upper()
        if sym not in wanted:
            continue
        parsed = _parse_akshare_sina_row(row, sym)
        if parsed:
            out[sym] = parsed
    return out


def _fetch_tushare_sync(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Tushare us_daily 最近交易日批量行情。"""
    import os
    from datetime import timedelta

    token = (os.environ.get("TUSHARE_TOKEN") or "").strip()
    if not token:
        return {}
    try:
        import tushare as ts
    except ImportError:
        logger.warning("Tushare not installed")
        return {}

    wanted = set(symbols)
    pro = ts.pro_api(token)
    now_et = datetime.now(_US_TZ)
    out: dict[str, dict[str, Any]] = {}
    for delta in range(0, 8):
        d = (now_et - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            df = pro.us_daily(trade_date=d, fields="ts_code,close,pct_change,vol")
        except Exception as exc:
            logger.warning("Tushare us_daily %s failed: %s", d, exc)
            continue
        if df is None or df.empty:
            continue
        for _, row in df.iterrows():
            code = str(row.get("ts_code") or "").upper().split(".")[0]
            if code not in wanted or code in out:
                continue
            price = _to_float(row.get("close"))
            if price is None:
                continue
            out[code] = _quote_row(
                code,
                name=code,
                price=price,
                change_pct=_to_float(row.get("pct_change")) or 0.0,
                volume=_to_float(row.get("vol")) or 0.0,
            )
        if out:
            break
    return out


def _tickdb_us_symbol(symbol: str) -> str:
    sym = symbol.upper().strip()
    return sym if sym.endswith(".US") else f"{sym}.US"


def _symbol_from_tickdb(code: str) -> str:
    text = str(code or "").upper().strip()
    if text.endswith(".US"):
        return text[:-3]
    return text.split(".")[0]


def _parse_tickdb_ticker(item: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    price = _to_float(item.get("last_price") or item.get("price") or item.get("last"))
    if price is None:
        return None
    change_pct = _to_float(
        item.get("price_change_percent_24h")
        or item.get("change_percent")
        or item.get("change_pct")
    )
    if change_pct is None:
        change = _to_float(item.get("price_change_24h") or item.get("change"))
        if change is not None and price:
            base = price - change
            change_pct = (change / base * 100) if base else 0.0
        else:
            change_pct = 0.0
    volume = _to_float(item.get("volume_24h") or item.get("volume")) or 0.0
    quote_time = None
    quote_time_et = None
    ts = item.get("timestamp")
    if ts:
        try:
            dt = datetime.fromtimestamp(int(ts) / 1000, tz=_US_TZ)
            quote_time_et = dt.strftime("%Y-%m-%d %H:%M:%S %Z")
            quote_time = dt.astimezone(_BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            pass
    return _quote_row(
        symbol,
        name=str(item.get("name") or symbol),
        price=price,
        change_pct=change_pct,
        volume=volume,
        quote_time=quote_time,
        quote_time_et=quote_time_et,
    )


async def _fetch_tickdb(symbols: list[str]) -> dict[str, dict[str, Any]]:
    from app.config import TICKDB_API_KEY

    token = (TICKDB_API_KEY or "").strip()
    if not token:
        return {}
    wanted = set(symbols)
    out: dict[str, dict[str, Any]] = {}
    headers = {"X-API-Key": token}
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        batch_count = (len(symbols) + TICKDB_BATCH_SIZE - 1) // TICKDB_BATCH_SIZE
        for bi, i in enumerate(range(0, len(symbols), TICKDB_BATCH_SIZE)):
            chunk = symbols[i : i + TICKDB_BATCH_SIZE]
            tick_syms = ",".join(_tickdb_us_symbol(s) for s in chunk)
            parsed_batch = False
            for attempt in range(5):
                try:
                    resp = await client.get(
                        TICKDB_TICKER,
                        params={"symbols": tick_syms, "type": "stock"},
                    )
                    if resp.status_code == 429:
                        wait = TICKDB_BATCH_PAUSE * (attempt + 2)
                        logger.warning("TickDB rate limited, retry in %.1fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status_code != 200:
                        logger.warning("TickDB ticker HTTP %s", resp.status_code)
                        break
                    body = resp.json()
                    code = body.get("code")
                    if code not in (0, "0", None):
                        logger.warning(
                            "TickDB ticker error code=%s msg=%s",
                            code,
                            body.get("message") or body.get("error"),
                        )
                        break
                    data = body.get("data") or []
                    if not isinstance(data, list):
                        break
                    for item in data:
                        if not isinstance(item, dict):
                            continue
                        sym = _symbol_from_tickdb(str(item.get("symbol") or ""))
                        if sym not in wanted or sym in out:
                            continue
                        parsed = _parse_tickdb_ticker(item, sym)
                        if parsed:
                            out[sym] = parsed
                    parsed_batch = True
                    break
                except Exception as exc:
                    logger.warning("TickDB ticker batch failed: %s", exc)
                    break
            if not parsed_batch:
                logger.warning("TickDB batch %s/%s skipped", bi + 1, batch_count)
            if bi + 1 < batch_count:
                await asyncio.sleep(TICKDB_BATCH_PAUSE)
    return out


async def _fetch_tushare(symbols: list[str]) -> dict[str, dict[str, Any]]:
    import asyncio

    if not symbols:
        return {}
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_fetch_tushare_sync, symbols),
            timeout=45,
        )
    except Exception as exc:
        logger.warning("Tushare fetch failed: %s", exc)
        return {}


async def warm_sina_spot_cache() -> bool:
    """预热新浪行情缓存（逐只日线）。"""
    got = await _fetch_akshare_sina_daily(["TSLA", "AAPL"])
    return bool(got)


async def _fetch_akshare(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """AKShare：东财 ulist → 新浪日线逐只 → 东财全表 →（可选）新浪全表。"""
    import asyncio

    if not symbols:
        return {}
    out = await _fetch_akshare_em_direct(symbols)
    missing = [s for s in symbols if s not in out]
    if missing:
        out.update(await _fetch_akshare_sina_daily(missing))
    missing = [s for s in symbols if s not in out]
    if not missing:
        return out
    try:
        df = await asyncio.wait_for(asyncio.to_thread(_akshare_us_spot_table), timeout=45)
        sym_col = "代码" if "代码" in df.columns else "symbol"
        for _, row in df.iterrows():
            sym = str(row.get(sym_col) or "").strip().upper()
            if sym not in missing:
                continue
            parsed = _parse_akshare_row(row, sym)
            if parsed:
                out[sym] = parsed
    except Exception as exc:
        logger.warning("AKShare em spot table failed: %s", exc)
    missing = [s for s in symbols if s not in out]
    if missing and os.environ.get("HEATMAP_SINA_FULL", "").strip().lower() in ("1", "true", "yes"):
        sina = await _fetch_akshare_sina(missing)
        out.update(sina)
    return out


def _is_quality_ok(quote_count: int, total: int) -> bool:
    if total <= 0:
        return False
    return quote_count >= max(20, int(total * _MIN_QUOTE_RATIO))


async def _fetch_quotes(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """TickDB → Yahoo → AKShare → Tushare（仅补缺）。"""
    from app.config import TICKDB_API_KEY

    sources_used: list[str] = []
    merged: dict[str, dict[str, Any]] = {}

    if (TICKDB_API_KEY or "").strip():
        tickdb = await _fetch_tickdb(symbols)
        if tickdb:
            merged.update(tickdb)
            sources_used.append(f"TickDB:{len(tickdb)}")

    missing = [s for s in symbols if s not in merged]
    skip_yahoo = os.environ.get("HEATMAP_SKIP_YAHOO", "").strip().lower() in ("1", "true", "yes")
    if missing and not skip_yahoo:
        yahoo = await _fetch_yahoo(missing)
        if yahoo:
            merged.update(yahoo)
            sources_used.append(f"Yahoo:{len(yahoo)}")
        missing = [s for s in symbols if s not in merged]

    if missing:
        ak = await _fetch_akshare(missing)
        if ak:
            merged.update(ak)
            sources_used.append(f"AKShare:{len(ak)}")
        missing = [s for s in symbols if s not in merged]

    if missing:
        ts = await _fetch_tushare(missing)
        if ts:
            merged.update(ts)
            sources_used.append(f"Tushare:{len(ts)}")

    label = "+".join(sources_used) if sources_used else "none"
    if not _is_quality_ok(len(merged), len(symbols)):
        logger.error(
            "heatmap quotes low quality %s/%s via %s",
            len(merged),
            len(symbols),
            label,
        )
    primary = label.split("+")[0].split(":")[0] if label != "none" else "none"
    return merged, primary if len(sources_used) == 1 else label


async def get_quotes_for_symbols(symbols: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """按 symbols 拉取报价，供 ai_mainline 等复用。返回 ({sym: quote_row}, source)。"""
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not uniq:
        return {}, "none"
    return await _fetch_quotes(uniq)


async def fetch_period_returns(
    symbols: list[str],
) -> dict[str, dict[str, float | None]]:
    """近 5/20 交易日累计涨跌（%）。优先 HeatmapSnapshot 收盘价，不足则 Yahoo chart。"""
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    out: dict[str, dict[str, float | None]] = {
        s: {"ret_5d": None, "ret_20d": None} for s in uniq
    }
    if not uniq:
        return out

    from_snap = _period_returns_from_snapshots(uniq)
    for sym, vals in from_snap.items():
        out[sym].update(vals)

    missing = [
        s
        for s in uniq
        if out[s].get("ret_5d") is None or out[s].get("ret_20d") is None
    ]
    if missing:
        from_yahoo = await _period_returns_from_yahoo(missing)
        for sym, vals in from_yahoo.items():
            cur = out.setdefault(sym, {"ret_5d": None, "ret_20d": None})
            if cur.get("ret_5d") is None and vals.get("ret_5d") is not None:
                cur["ret_5d"] = vals["ret_5d"]
            if cur.get("ret_20d") is None and vals.get("ret_20d") is not None:
                cur["ret_20d"] = vals["ret_20d"]
    return out


def _period_ret_from_closes(closes: list[float], trading_days: int) -> float | None:
    """closes 按时间升序；trading_days=5 表示约 5 个交易日涨跌。"""
    if len(closes) < trading_days + 1:
        return None
    start = closes[-(trading_days + 1)]
    end = closes[-1]
    if not start:
        return None
    return round((end - start) / start * 100, 2)


def _period_returns_from_snapshots(
    symbols: list[str],
) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    try:
        from sqlalchemy import desc

        from app.database import HeatmapSnapshot, SessionLocal

        with SessionLocal() as db:
            dates = [
                r[0]
                for r in (
                    db.query(HeatmapSnapshot.trade_date)
                    .order_by(desc(HeatmapSnapshot.trade_date))
                    .distinct()
                    .limit(25)
                    .all()
                )
            ]
            if len(dates) < 2:
                return out
            since = dates[-1]
            rows = (
                db.query(HeatmapSnapshot)
                .filter(
                    HeatmapSnapshot.kind == "company",
                    HeatmapSnapshot.symbol.in_(symbols),
                    HeatmapSnapshot.trade_date >= since,
                )
                .order_by(HeatmapSnapshot.trade_date)
                .all()
            )
        by_sym: dict[str, list[float]] = {}
        for row in rows:
            if row.price and row.price > 0:
                by_sym.setdefault(row.symbol.upper(), []).append(float(row.price))
        for sym, closes in by_sym.items():
            out[sym] = {
                "ret_5d": _period_ret_from_closes(closes, 5),
                "ret_20d": _period_ret_from_closes(closes, 20),
            }
    except Exception as exc:
        logger.warning("period returns from snapshots failed: %s", exc)
    return out


async def _period_returns_from_yahoo(
    symbols: list[str],
) -> dict[str, dict[str, float | None]]:
    import asyncio

    out: dict[str, dict[str, float | None]] = {}
    headers = {**HEADERS, "Referer": "https://finance.yahoo.com/"}
    sem = asyncio.Semaphore(6)

    async def one(client: httpx.AsyncClient, symbol: str):
        ysym = _yahoo_symbol(symbol)
        try:
            async with sem:
                resp = await client.get(
                    YAHOO_CHART.format(symbol=ysym),
                    params={"interval": "1d", "range": "1mo"},
                )
            if resp.status_code != 200:
                return symbol, None
            result = ((resp.json().get("chart") or {}).get("result")) or []
            if not result:
                return symbol, None
            quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            closes = [float(c) for c in (quote.get("close") or []) if c is not None]
            if len(closes) < 2:
                return symbol, None
            return symbol, {
                "ret_5d": _period_ret_from_closes(closes, 5),
                "ret_20d": _period_ret_from_closes(closes, 20),
            }
        except Exception as exc:
            logger.debug("Yahoo period failed %s: %s", symbol, exc)
            return symbol, None

    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        results = await asyncio.gather(*[one(client, s) for s in symbols])
    for sym, vals in results:
        if vals:
            out[sym] = vals
    return out


def _heatmap_failure(source: str, quote_count: int, total: int) -> dict[str, Any]:
    from app.config import TICKDB_API_KEY

    hint = (
        "请在 .env 配置 TICKDB_API_KEY（https://tickdb.ai 免费注册）。"
        if not (TICKDB_API_KEY or "").strip()
        else "已尝试 TickDB / Yahoo / AKShare / Tushare，请稍后点击刷新。"
    )
    note = f"行情拉取失败（{source} 仅 {quote_count}/{total}）。{hint}"
    return {
        "success": False,
        **_response_timestamps(),
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
        "「实时」= 当前刷新行情，排行随盘前/盘中/盘后变化；"
        "「今日」= 当前美东交易日，收盘快照在美东 16:30 后入库用于周期统计。"
        "本站% = 占监控样本（约11个主要板块+龙头股）的资金活跃度比重，"
        "非真实 Level2 资金流；红流入绿流出。"
        "主题涨跌幅 = 有代表性 ETF 的主题优先使用 ETF 实时价格，其余为成分股等权平均。"
    )

    return {
        "success": True,
        "mode": "live",
        **_response_timestamps(by_symbol),
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

    lock = _heatmap_build_lock()
    async with lock:
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
        _CACHE["ts"] = time.time()
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
    """拉取行情并写入收盘快照（按美东交易日一条）。

    自动任务在美东 16:30 触发；也可手动「存今日快照」补数据。
    快照内容为该美东交易日收盘时点的涨跌幅与全日成交量，非盘前/盘中实时。
    """
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


def _stats_timestamps() -> dict[str, str]:
    now_et = datetime.now(_US_TZ)
    now_bj = now_et.astimezone(_BJ_TZ)
    return {
        "updated_at": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_bj": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
        "market_time_bj": now_bj.strftime("%Y-%m-%d %H:%M:%S"),
        "market_time_et": now_et.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_period_stats(period: str = "1w") -> dict[str, Any]:
    """按周期统计板块/公司资金变化（依赖美东交易日收盘快照）。

    「每日」(1d) = 仅取最近一个美东交易日 16:30 入库的收盘快照。
    更长周期 = 区间内各交易日收盘快照的 flow_score 累计。
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
    rank_label = cfg.get("rank_label", label)
    period_desc = cfg.get("desc", "")
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

    # 「每日」只展示最近一个美东交易日收盘快照，不累加多日
    if period == "1d" and latest:
        latest_date = latest[0]
        records = [r for r in records if r.trade_date == latest_date]

    dates = sorted({r.trade_date.isoformat() for r in records})
    snapshot_count = len(dates)
    latest_trade_date = latest[0].isoformat() if latest else None
    enough = snapshot_count >= min_snapshots

    empty = {
        "period": period,
        "label": label,
        "rank_label": rank_label,
        "period_desc": period_desc,
        "snapshot_time_desc": SNAPSHOT_TIME_DESC,
        "mode": "period",
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
        "periods": _periods_for_api(),
        **_stats_timestamps(),
    }

    if snapshot_count == 0:
        empty["note"] = (
            f"「{label}」暂无收盘快照。"
            f"{period_desc} "
            "请先点击「存今日快照」，"
            f"或等待{SNAPSHOT_TIME_DESC}；积累够天数后再查看更长周期。"
        )
        return empty

    if not enough:
        empty["note"] = (
            f"「{label}」数据还不足：该周期至少需要 {min_snapshots} 个交易日快照，"
            f"当前只有 {snapshot_count} 天"
            + (f"（最新美东交易日 {latest_trade_date}）" if latest_trade_date else "")
            + "。可先查看「每日」或「实时行情」；"
            f"请继续每日在{SNAPSHOT_TIME_DESC}后自动积累，或手动存快照。"
        )
        return empty

    sectors = _aggregate_period(records, "sector")
    companies = _aggregate_period(records, "company")

    return {
        "period": period,
        "label": label,
        "rank_label": rank_label,
        "period_desc": period_desc,
        "snapshot_time_desc": SNAPSHOT_TIME_DESC,
        "mode": "period",
        "days": days,
        "min_snapshots": min_snapshots,
        "since": since.isoformat(),
        "snapshot_dates": dates,
        "snapshot_count": snapshot_count,
        "latest_trade_date": latest_trade_date,
        "sufficient": True,
        "note": (
            f"时间口径：{period_desc} "
            "本站% = 占监控样本（主要板块+龙头股）的资金活跃度比重，非全市场。"
            + (
                "排行 = 该交易日收盘快照的 flow_score。"
                if period == "1d"
                else "排行 = 区间内各交易日 flow_score 累计；悬停可看股价涨跌。"
            )
        ),
        **_stats_timestamps(),
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
        "periods": _periods_for_api(),
    }
