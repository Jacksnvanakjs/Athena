"""合作快讯受益方首日股价回测：自动打档 / 回测分，中·低计异常。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.utils import now_beijing

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# 与 canvas 一致：发稿后首个交易日收盘 vs 前收
BAND_HIGH = "高"
BAND_MID_HIGH = "中高"
BAND_MID = "中"
BAND_LOW = "低"
BAND_NONE = "无行情"

SCORE_BY_BAND = {
    BAND_HIGH: 85,
    BAND_MID_HIGH: 70,
    BAND_MID: 55,
    BAND_LOW: 35,
}

# 用户要求：中、低计异常；档位标识本身不变
ANOMALY_BANDS = frozenset({BAND_MID, BAND_LOW})


@dataclass
class FirstDayOutcome:
    ret: float | None
    band: str
    score: int | None
    session_date: date | None
    anomaly: bool
    note: str


def score_first_day_return(ret: float | None) -> FirstDayOutcome:
    """≥+3% 高(85)，+1%~+3% 中高(70)，±1% 中(55)，≤-1% 低(35)。"""
    if ret is None:
        return FirstDayOutcome(
            ret=None,
            band=BAND_NONE,
            score=None,
            session_date=None,
            anomaly=False,
            note="无日线",
        )
    pct = ret * 100.0
    if pct >= 3.0:
        band = BAND_HIGH
    elif pct >= 1.0:
        band = BAND_MID_HIGH
    elif pct > -1.0:
        band = BAND_MID
    else:
        band = BAND_LOW
    return FirstDayOutcome(
        ret=ret,
        band=band,
        score=SCORE_BY_BAND[band],
        session_date=None,
        anomaly=band in ANOMALY_BANDS,
        note="",
    )


def _published_et(published_at: datetime) -> datetime:
    if published_at.tzinfo is None:
        aware = published_at.replace(tzinfo=timezone.utc)
    else:
        aware = published_at
    return aware.astimezone(ET)


def reaction_start_date(published_at: datetime) -> date:
    """可交易首日的起始日历日（美东）。

    目的：模拟「看到新闻后最早能买、并吃到的那一节交易日涨幅」。
    - 盘中/盘前（美东开盘前～16:00 前）发稿 → 用**当天**这根日 K（开盘后可买）
    - 盘后（美东 ≥16:00）或已收盘 → 用**下一自然日**起的首个有收盘的交易日
      （周末/假日由日线列表自动跳到下一交易日）
    """
    et = _published_et(published_at)
    if et.hour >= 16:
        return (et + timedelta(days=1)).date()
    return et.date()


async def compute_first_day_move(
    ticker: str,
    published_at: datetime,
) -> FirstDayOutcome:
    """新闻后第一个可交易日：收盘相对前收（买得越早越接近吃满这根 K）。"""
    from app.market_data import fetch_daily_closes

    ticker = (ticker or "").upper().strip()
    if not ticker or not published_at:
        return score_first_day_return(None)

    start = reaction_start_date(published_at)
    closes = await fetch_daily_closes(ticker, lookback_days=40)
    if len(closes) < 2:
        out = score_first_day_return(None)
        out.note = "无日线"
        return out

    idx = None
    for i, (d, _c) in enumerate(closes):
        if d >= start:
            idx = i
            break
    if idx is None:
        out = score_first_day_return(None)
        out.note = "待收盘（新闻后首个交易日尚未结束）"
        out.band = BAND_NONE
        return out
    if idx < 1:
        out = score_first_day_return(None)
        out.note = "缺前收"
        return out

    prev_close = closes[idx - 1][1]
    day_close = closes[idx][1]
    session_d = closes[idx][0]
    if not prev_close or prev_close <= 0:
        out = score_first_day_return(None)
        out.note = "缺前收"
        return out

    ret = (day_close - prev_close) / prev_close
    out = score_first_day_return(ret)
    out.session_date = session_d
    et = _published_et(published_at)
    timing = "盘后→下一交易日" if et.hour >= 16 else "当日盘前/盘中"
    out.note = f"{session_d.isoformat()} 收盘 vs 前收（{timing}）"
    return out


def apply_first_day_to_event(event, outcome: FirstDayOutcome) -> None:
    event.first_day_return = outcome.ret
    event.first_day_band = outcome.band
    event.first_day_score = outcome.score
    event.first_day_session_date = outcome.session_date
    event.first_day_anomaly = bool(outcome.anomaly)
    event.first_day_note = (outcome.note or "")[:200]
    event.first_day_checked_at = now_beijing()


async def refresh_event_first_day(event) -> FirstDayOutcome:
    ticker = getattr(event, "beneficiary_ticker", None) or ""
    published = getattr(event, "published_at", None)
    outcome = await compute_first_day_move(ticker, published)
    apply_first_day_to_event(event, outcome)
    return outcome


async def run_first_day_check(*, lookback_days: int = 90, force: bool = False) -> dict:
    """回填 deal + nvda 受益方首日涨跌与档位。"""
    from app.database import DealEvent, NvdaSignalEvent, db_session
    from app.source_url_guard import is_test_source_url

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, lookback_days))
    summary = {
        "checked": 0,
        "updated": 0,
        "anomalies": 0,
        "pending": 0,
        "errors": [],
    }

    with db_session() as db:
        deals = (
            db.query(DealEvent)
            .filter(DealEvent.published_at >= cutoff)
            .order_by(DealEvent.id.desc())
            .all()
        )
        # 含当前被内容过滤隐藏的条目：先回填首日，再由 should_hide 按涨跌原则放行
        deals = [e for e in deals if not is_test_source_url(e.source_url)]
        nvdas = (
            db.query(NvdaSignalEvent)
            .filter(NvdaSignalEvent.published_at >= cutoff)
            .order_by(NvdaSignalEvent.id.desc())
            .all()
        )
        nvdas = [e for e in nvdas if not is_test_source_url(e.source_url)]

        for event in (*deals, *nvdas):
            need = force or event.first_day_checked_at is None or (
                event.first_day_band in (None, BAND_NONE)
                or event.first_day_return is None
            )
            try:
                if need:
                    outcome = await refresh_event_first_day(event)
                    summary["updated"] += 1
                    if outcome.band == BAND_NONE:
                        summary["pending"] += 1
                    db.commit()
                summary["checked"] += 1
                if event.first_day_anomaly:
                    summary["anomalies"] += 1
            except Exception as exc:
                db.rollback()
                logger.exception(
                    "首日回测失败 id=%s %s",
                    getattr(event, "id", None),
                    getattr(event, "beneficiary_ticker", ""),
                )
                summary["errors"].append(str(exc)[:160])

    logger.info("deal first_day check: %s", summary)
    return summary
