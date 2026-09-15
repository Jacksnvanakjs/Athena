import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import holidays

from app.config import TIMEZONE

_TZ = ZoneInfo(TIMEZONE)
_US_TZ = ZoneInfo("America/New_York")


@dataclass
class FundInfo:
    name: str
    code: str
    url: str


def now_beijing() -> datetime:
    """返回无时区信息的北京时间，便于写入 SQLite/Turso 并按中国时间展示。"""
    return datetime.now(_TZ).replace(tzinfo=None)


def today_beijing() -> date:
    return datetime.now(_TZ).date()


def today_us() -> date:
    return datetime.now(_US_TZ).date()


def last_completed_us_session(as_of: datetime | None = None) -> date:
    """最近一个已收盘的美东交易日。16:00 ET 前仍算上一交易日。"""
    now = as_of or datetime.now(_US_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_US_TZ)
    else:
        now = now.astimezone(_US_TZ)
    d = now.date()
    if now.hour < 16:
        d -= timedelta(days=1)
    guard = 0
    while not is_us_trading_day(d) and guard < 14:
        d -= timedelta(days=1)
        guard += 1
    return d


def is_us_trading_day(check_date: date | None = None) -> bool:
    check_date = check_date or today_us()
    if check_date.weekday() >= 5:
        return False
    return check_date not in holidays.US(years=check_date.year)


def load_funds(source_file: str) -> list[FundInfo]:
    funds = []
    path = Path(source_file)
    if not path.exists():
        return funds

    for line in path.read_text(encoding="utf-8").strip().splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.match(r"(.+?)\((\d{6})\)\s+(https?://\S+)", line)
        if match:
            funds.append(FundInfo(name=match.group(1), code=match.group(2), url=match.group(3)))
    return funds


def is_trading_day(check_date: date | None = None) -> bool:
    check_date = check_date or today_beijing()
    if check_date.weekday() >= 5:
        return False

    cn_holidays = holidays.China(years=check_date.year)
    us_holidays = holidays.US(years=check_date.year)

    return check_date not in cn_holidays and check_date not in us_holidays


def parse_quota_status(status_text: str) -> tuple[str, float]:
    text = status_text.strip()
    text = re.sub(r"\s+", " ", text)

    if "暂停申购" in text:
        return "暂停申购", 0.0

    if "限大额" in text:
        match = re.search(r"单日累计购买上限\s*([\d.]+)\s*元", text)
        if match:
            return "限大额", float(match.group(1))
        return "限大额", 0.0

    return "未知", 0.0
