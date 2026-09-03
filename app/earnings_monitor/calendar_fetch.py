"""拉取下次财报日：Finnhub 优先（按标的），Yahoo 并行备选。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.earnings_monitor.config import EARNINGS_LOOKAHEAD_DAYS, FINNHUB_API_KEY
from app.earnings_monitor.trade_window import today_bj

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


@dataclass
class CalendarHit:
    ticker: str
    earnings_date: date
    session: str  # BMO / AMC / TBD
    confirmed: bool
    source: str


def today_et() -> date:
    """美东日历日（对接 Finnhub/Nasdaq 等美股源时可用）。"""
    return datetime.now(_ET).date()


def _today_fetch_start() -> date:
    """抓取起点：北京今天往前 1 天，避免漏掉跨日盘后日程。"""
    return today_bj() - timedelta(days=1)

def _parse_session(raw: str | None) -> str:
    s = (raw or "").strip().upper()
    if s in ("BMO", "AMC"):
        return s
    low = (raw or "").lower()
    if "before" in low or "bmo" in low:
        return "BMO"
    if "after" in low or "amc" in low:
        return "AMC"
    return "TBD"


def _parse_date(raw) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(int(raw), tz=_ET).date()
        except (OSError, OverflowError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


async def _finnhub_symbol(
    client: httpx.AsyncClient,
    ticker: str,
    start: date,
    end: date,
) -> CalendarHit | None:
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {
        "symbol": ticker,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "token": FINNHUB_API_KEY,
    }
    for attempt in range(2):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            rows = data.get("earningsCalendar") or []
            best: CalendarHit | None = None
            for row in rows:
                ed = _parse_date(row.get("date"))
                if not ed or ed < start or ed > end:
                    continue
                session = _parse_session(str(row.get("hour") or ""))
                hit = CalendarHit(
                    ticker=ticker.upper(),
                    earnings_date=ed,
                    session=session,
                    confirmed=session in ("BMO", "AMC"),
                    source="finnhub",
                )
                if not best or ed < best.earnings_date:
                    best = hit
            return best
        except Exception as exc:
            logger.debug("Finnhub earnings %s: %s", ticker, exc)
            return None
    return None


async def fetch_finnhub_calendar(
    tickers: set[str],
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[CalendarHit]:
    if not FINNHUB_API_KEY:
        return []
    start = from_date or _today_fetch_start()
    end = to_date or (start + timedelta(days=EARNINGS_LOOKAHEAD_DAYS))

    # 先尝试批量区间（省配额）；429/失败再按标的
    url = "https://finnhub.io/api/v1/calendar/earnings"
    params = {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "token": FINNHUB_API_KEY,
    }
    hits: list[CalendarHit] = []
    try:
        async with httpx.AsyncClient(timeout=40) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                rows = (resp.json().get("earningsCalendar") or [])
                for row in rows:
                    sym = str(row.get("symbol") or "").strip().upper()
                    if sym not in tickers:
                        continue
                    ed = _parse_date(row.get("date"))
                    if not ed:
                        continue
                    session = _parse_session(str(row.get("hour") or ""))
                    hits.append(
                        CalendarHit(
                            ticker=sym,
                            earnings_date=ed,
                            session=session,
                            confirmed=session in ("BMO", "AMC"),
                            source="finnhub",
                        )
                    )
            elif resp.status_code == 429:
                logger.warning("Finnhub earnings calendar 429，改按标的拉取")
            else:
                logger.warning("Finnhub earnings calendar HTTP %s", resp.status_code)
    except Exception as exc:
        logger.warning("Finnhub earnings calendar 失败: %s", exc)

    found = {h.ticker for h in hits}
    missing = sorted(tickers - found)
    # 批量已 429 时按标的再打容易继续限流，交给 Nasdaq/Yahoo
    if missing and FINNHUB_API_KEY and hits:
        sem = asyncio.Semaphore(3)

        async def _one(t: str) -> CalendarHit | None:
            async with sem:
                async with httpx.AsyncClient(timeout=25) as client:
                    return await _finnhub_symbol(client, t, start, end)

        parts = await asyncio.gather(*[_one(t) for t in missing])
        for hit in parts:
            if hit:
                hits.append(hit)

    return hits


async def fetch_yahoo_next_earnings(ticker: str) -> CalendarHit | None:
    url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
    params = {"modules": "calendarEvents"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            result = (resp.json().get("quoteSummary") or {}).get("result") or []
            if not result:
                return None
            earnings = (result[0].get("calendarEvents") or {}).get("earnings") or {}
            dates = earnings.get("earningsDate") or []
            if not dates:
                return None
            raw = dates[0]
            ts = raw.get("raw") if isinstance(raw, dict) else None
            ed = _parse_date(ts if ts is not None else raw)
            if not ed:
                return None
            if ed < _today_fetch_start():
                return None
            if ed > today_bj() + timedelta(days=EARNINGS_LOOKAHEAD_DAYS + 1):
                return None
            return CalendarHit(
                ticker=ticker.upper(),
                earnings_date=ed,
                session="TBD",
                confirmed=False,
                source="yahoo",
            )
    except Exception as exc:
        logger.debug("Yahoo earnings %s 失败: %s", ticker, exc)
        return None


async def fetch_nasdaq_calendar(
    tickers: set[str],
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[CalendarHit]:
    """Nasdaq 公开财报日历（按日）；Yahoo 403 时的稳定备选。"""
    start = from_date or _today_fetch_start()
    end = to_date or (start + timedelta(days=min(EARNINGS_LOOKAHEAD_DAYS, 60)))
    days: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)

    headers = {
        "User-Agent": "Mozilla/5.0 AthenaEarningsMonitor/1.0",
        "Accept": "application/json",
    }
    sem = asyncio.Semaphore(6)
    hits: list[CalendarHit] = []

    async def _day(client: httpx.AsyncClient, day: date) -> list[CalendarHit]:
        async with sem:
            try:
                resp = await client.get(
                    "https://api.nasdaq.com/api/calendar/earnings",
                    params={"date": day.isoformat()},
                )
                if resp.status_code != 200:
                    return []
                rows = ((resp.json().get("data") or {}).get("rows")) or []
                out: list[CalendarHit] = []
                for row in rows:
                    sym = str(row.get("symbol") or "").strip().upper()
                    if sym not in tickers:
                        continue
                    time_raw = str(row.get("time") or "")
                    session = "AMC" if "after" in time_raw else ("BMO" if "before" in time_raw else "TBD")
                    out.append(
                        CalendarHit(
                            ticker=sym,
                            earnings_date=day,
                            session=session,
                            confirmed=session in ("BMO", "AMC"),
                            source="nasdaq",
                        )
                    )
                return out
            except Exception as exc:
                logger.debug("Nasdaq earnings %s: %s", day, exc)
                return []

    async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=True) as client:
        batches = await asyncio.gather(*[_day(client, d) for d in days])
    for batch in batches:
        hits.extend(batch)
    return hits


async def fetch_calendar_for_universe(tickers: list[str]) -> list[CalendarHit]:
    ticker_set = {t.upper() for t in tickers}
    hits = await fetch_finnhub_calendar(ticker_set)
    found = {h.ticker for h in hits}
    missing = ticker_set - found

    if missing:
        nasdaq_hits = await fetch_nasdaq_calendar(missing)
        for hit in nasdaq_hits:
            hits.append(hit)
        found = {h.ticker for h in hits}
        missing = ticker_set - found

    if missing:
        sem = asyncio.Semaphore(5)

        async def _yahoo(t: str) -> CalendarHit | None:
            async with sem:
                return await fetch_yahoo_next_earnings(t)

        parts = await asyncio.gather(*[_yahoo(t) for t in sorted(missing)])
        for hit in parts:
            if hit:
                hits.append(hit)

    logger.info(
        "earnings calendar sources: total=%d finnhub=%d nasdaq=%d yahoo=%d",
        len({h.ticker for h in hits}),
        sum(1 for h in hits if h.source == "finnhub"),
        sum(1 for h in hits if h.source == "nasdaq"),
        sum(1 for h in hits if h.source == "yahoo"),
    )

    best: dict[str, CalendarHit] = {}
    for h in hits:
        if h.earnings_date < _today_fetch_start():
            continue
        prev = best.get(h.ticker)
        if not prev or h.earnings_date < prev.earnings_date:
            best[h.ticker] = h
    return list(best.values())
