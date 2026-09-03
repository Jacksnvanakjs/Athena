from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from app.config import CHANGE_HIGHLIGHT_DAYS, FUNDS_SOURCE_FILE, SCRAPE_SECRET, SCRAPE_TIMES, TIMEZONE, USE_TURSO
from app.database import Fund, QuotaRecord, run_with_db_retry
from app.nvda_signal.trade_window import strategy_label
from app.source_url_guard import is_test_source_url
from app.service import run_scrape_and_notify
from app.time_display import (
    format_beijing_at_bj,
    format_beijing_at_display,
    format_beijing_at_et,
    format_published_at_bj,
    format_published_at_et,
)
from app.utils import is_trading_day, now_beijing

router = APIRouter(prefix="/api")


def _check_scrape_secret(secret: str | None):
    if SCRAPE_SECRET and secret != SCRAPE_SECRET:
        raise HTTPException(status_code=403, detail="无效的密钥")


def _recent_change_baseline(records_desc: list[QuotaRecord]) -> tuple[float | None, str | None, str | None]:
    """在最近 CHANGE_HIGHLIGHT_DAYS 天内找到最近一次额度/状态变化，返回变化前的值与时间。"""
    if len(records_desc) < 2:
        return None, None, None
    since = now_beijing() - timedelta(days=CHANGE_HIGHLIGHT_DAYS)
    for i in range(len(records_desc) - 1):
        curr = records_desc[i]
        prev = records_desc[i + 1]
        if curr.quota != prev.quota or curr.status != prev.status:
            if curr.scraped_at >= since:
                return prev.quota, prev.status, curr.scraped_at.isoformat(timespec="minutes")
            return None, None, None
    return None, None, None


def _list_funds(db: Session):
    funds = db.query(Fund).all()
    since = now_beijing() - timedelta(days=CHANGE_HIGHLIGHT_DAYS)
    result = []
    for fund in funds:
        latest = (
            db.query(QuotaRecord)
            .filter(QuotaRecord.fund_code == fund.code)
            .order_by(desc(QuotaRecord.scraped_at))
            .first()
        )
        records = (
            db.query(QuotaRecord)
            .filter(QuotaRecord.fund_code == fund.code, QuotaRecord.scraped_at >= since)
            .order_by(desc(QuotaRecord.scraped_at))
            .all()
        )
        older = (
            db.query(QuotaRecord)
            .filter(QuotaRecord.fund_code == fund.code, QuotaRecord.scraped_at < since)
            .order_by(desc(QuotaRecord.scraped_at))
            .first()
        )
        if older:
            records.append(older)
        prev_quota, prev_status, changed_at = _recent_change_baseline(records)
        result.append(
            {
                "code": fund.code,
                "name": fund.name,
                "url": fund.url,
                "status": latest.status if latest else None,
                "quota": latest.quota if latest else None,
                "scraped_at": latest.scraped_at.isoformat() if latest else None,
                "previous_status": prev_status,
                "previous_quota": prev_quota,
                "changed_at": changed_at,
            }
        )
    return result


@router.get("/funds")
def list_funds():
    return run_with_db_retry(_list_funds)


@router.get("/history/{fund_code}")
def fund_history(
    fund_code: str,
    days: int = Query(default=30, ge=1, le=730),
):
    since = now_beijing() - timedelta(days=days)

    def _query(db: Session):
        records = (
            db.query(QuotaRecord)
            .filter(QuotaRecord.fund_code == fund_code, QuotaRecord.scraped_at >= since)
            .order_by(QuotaRecord.scraped_at)
            .all()
        )
        return [
            {
                "quota": r.quota,
                "status": r.status,
                "scraped_at": r.scraped_at.isoformat(),
            }
            for r in records
        ]

    return run_with_db_retry(_query)


@router.get("/history")
def all_history(
    days: int = Query(default=30, ge=1, le=730),
):
    since = now_beijing() - timedelta(days=days)

    def _query(db: Session):
        records = (
            db.query(QuotaRecord)
            .filter(QuotaRecord.scraped_at >= since)
            .order_by(QuotaRecord.scraped_at)
            .all()
        )
        return [
            {
                "fund_code": r.fund_code,
                "fund_name": r.fund_name,
                "quota": r.quota,
                "status": r.status,
                "scraped_at": r.scraped_at.isoformat(),
            }
            for r in records
        ]

    return run_with_db_retry(_query)


@router.post("/scrape")
async def trigger_scrape():
    """网页「立即抓取」按钮调用，无需密钥"""
    result = await run_scrape_and_notify(FUNDS_SOURCE_FILE)
    return result


@router.get("/cron/scrape")
async def cron_scrape(secret: str = Query(...)):
    """供外部 cron 服务调用（如 cron-job.org），北京时间 11:05 / 14:35 触发"""
    _check_scrape_secret(secret)
    if not is_trading_day():
        return {"success": True, "skipped": True, "reason": "非交易日"}
    result = await run_scrape_and_notify(FUNDS_SOURCE_FILE)
    return result


@router.get("/status")
def system_status():
    from app.config import (
        AI_MAINLINE_ENABLED,
        DEAL_POLL_INTERVAL_MIN,
        ENABLE_SCHEDULER,
        EARNINGS_MONITOR_ENABLED,
        NVDA_SIGNAL_ENABLED,
    )
    from app.database import DealEvent, EarningsEvent, NvdaSignalEvent
    from app.deal_monitor.content_filter import should_hide_deal_event

    def _counts(db: Session):
        deals = [
            e for e in db.query(DealEvent).all()
            if not is_test_source_url(e.source_url) and not should_hide_deal_event(e)
        ]
        nvda = [e for e in db.query(NvdaSignalEvent).all() if not is_test_source_url(e.source_url)]
        earn = db.query(EarningsEvent).filter(EarningsEvent.status.in_(["upcoming", "pushed"])).count()
        return len(deals), len(nvda), earn

    from app.scheduler import scheduler_status

    deal_total, nvda_total, earnings_total = run_with_db_retry(_counts)
    sched = scheduler_status()
    return {
        "is_trading_day": is_trading_day(),
        "scrape_times": [f"{h:02d}:{m:02d}" for h, m in SCRAPE_TIMES],
        "timezone": TIMEZONE,
        "database": "turso" if USE_TURSO else "sqlite",
        "now": now_beijing().isoformat(timespec="seconds"),
        "scheduler_enabled": ENABLE_SCHEDULER,
        "scheduler_running": sched["running"],
        "scheduler": sched,
        "deal_monitor": {
            "poll_interval_min": DEAL_POLL_INTERVAL_MIN,
            "events_total": deal_total,
            "nvda_signal_total": nvda_total,
            "nvda_signal_enabled": NVDA_SIGNAL_ENABLED,
            "earnings_total": earnings_total,
            "earnings_enabled": EARNINGS_MONITOR_ENABLED,
            "ai_mainline_enabled": AI_MAINLINE_ENABLED,
        },
    }


@router.get("/heatmap")
async def heatmap_data(force: bool = Query(default=False)):
    """美股板块/个股热力图数据（缓存约 3 分钟）"""
    from app.heatmap import get_heatmap_data

    return await get_heatmap_data(force=force)


@router.get("/heatmap/stats")
def heatmap_stats(period: str = Query(default="1w")):
    """多周期资金变化统计：1d=每日(最近美东收盘) / 1w / 15d / 1m / 2m / 3m"""
    from app.heatmap import PERIODS, get_period_stats

    if period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"period 支持: {', '.join(PERIODS)}")
    return get_period_stats(period)


@router.post("/heatmap/snapshot")
async def heatmap_snapshot(force: bool = Query(default=True)):
    """手动保存今日美股热力图快照（用于补数据或本地测试）"""
    from app.heatmap import save_daily_snapshot

    return await save_daily_snapshot(force=force)


# ── AI 合作快讯 + 黄仁勋动向 ──

FEED_AI = "ai_cooperation"
FEED_NVDA = "nvda_signal"


def _push_ok(event) -> bool:
    return bool(
        event.pushed_at
        and event.push_channel
        and event.push_channel
        not in {"none", "failed", "unconfigured", "disabled", "rate_limited", "stale", "soft_skip"}
    )


def _first_day_fields(event) -> dict:
    ret = getattr(event, "first_day_return", None)
    return {
        "first_day_return": ret,
        "first_day_return_pct": None if ret is None else round(ret * 100.0, 2),
        "first_day_band": getattr(event, "first_day_band", None),
        "first_day_score": getattr(event, "first_day_score", None),
        "first_day_session_date": (
            event.first_day_session_date.isoformat()
            if getattr(event, "first_day_session_date", None)
            else None
        ),
        "first_day_anomaly": bool(getattr(event, "first_day_anomaly", False)),
        "first_day_note": getattr(event, "first_day_note", None),
        "first_day_checked_at": (
            event.first_day_checked_at.isoformat()
            if getattr(event, "first_day_checked_at", None)
            else None
        ),
    }


def _deal_to_dict(event) -> dict:
    return {
        "id": event.id,
        "category": FEED_AI,
        "category_label": "AI合作",
        "published_at": event.published_at.isoformat() if event.published_at else None,
        "published_at_bj": format_published_at_bj(event.published_at),
        "published_at_et": format_published_at_et(event.published_at),
        "fetched_at": event.fetched_at.isoformat() if event.fetched_at else None,
        "fetched_at_bj": format_beijing_at_bj(event.fetched_at),
        "fetched_at_et": format_beijing_at_et(event.fetched_at),
        "fetched_at_display": format_beijing_at_display(event.fetched_at),
        "headline": event.headline,
        "summary": event.summary,
        "source": event.source,
        "source_url": event.source_url,
        "anchor_name": event.anchor_name,
        "anchor_ticker": event.anchor_ticker,
        "anchor_tier": event.anchor_tier,
        "beneficiary_ticker": event.beneficiary_ticker,
        "beneficiary_name": event.beneficiary_name,
        "beneficiary_tier": event.beneficiary_tier,
        "beneficiary_market_cap_usd": event.beneficiary_market_cap_usd,
        "tier_pair": event.tier_pair,
        "materiality_score": event.materiality_score,
        "matched_keywords": event.matched_keywords,
        "event_type": event.event_type,
        "is_update": event.is_update,
        "pushed_at": event.pushed_at.isoformat() if event.pushed_at else None,
        "pushed_at_bj": format_beijing_at_bj(event.pushed_at) if event.pushed_at else None,
        "pushed_at_et": format_beijing_at_et(event.pushed_at) if event.pushed_at else None,
        "pushed_at_display": format_beijing_at_display(event.pushed_at) if event.pushed_at else None,
        "push_channel": event.push_channel,
        "pushed": _push_ok(event),
        "signal_tier": None,
        "strategy": None,
        "buy_window": None,
        "sell_window": None,
        "buy_ok": None,
        "confidence": None,
        **_first_day_fields(event),
    }


def _nvda_to_dict(event) -> dict:
    return {
        "id": event.id,
        "category": FEED_NVDA,
        "category_label": "黄仁勋",
        "published_at": event.published_at.isoformat() if event.published_at else None,
        "published_at_bj": format_published_at_bj(event.published_at),
        "published_at_et": format_published_at_et(event.published_at),
        "fetched_at": event.fetched_at.isoformat() if event.fetched_at else None,
        "fetched_at_bj": format_beijing_at_bj(event.fetched_at),
        "fetched_at_et": format_beijing_at_et(event.fetched_at),
        "fetched_at_display": format_beijing_at_display(event.fetched_at),
        "headline": event.headline,
        "summary": event.summary,
        "source": event.source,
        "source_url": event.source_url,
        "anchor_name": "NVIDIA",
        "anchor_ticker": "NVDA",
        "anchor_tier": "T0",
        "beneficiary_ticker": event.beneficiary_ticker,
        "beneficiary_name": event.beneficiary_name,
        "beneficiary_tier": event.beneficiary_tier,
        "beneficiary_market_cap_usd": event.beneficiary_market_cap_usd,
        "tier_pair": event.signal_tier,
        "materiality_score": event.materiality_score,
        "matched_keywords": event.action_type,
        "event_type": event.action_type,
        "is_update": False,
        "pushed_at": event.pushed_at.isoformat() if event.pushed_at else None,
        "pushed_at_bj": format_beijing_at_bj(event.pushed_at) if event.pushed_at else None,
        "pushed_at_et": format_beijing_at_et(event.pushed_at) if event.pushed_at else None,
        "pushed_at_display": format_beijing_at_display(event.pushed_at) if event.pushed_at else None,
        "push_channel": event.push_channel,
        "pushed": _push_ok(event),
        "signal_tier": event.signal_tier,
        "strategy": event.strategy,
        "buy_window": event.buy_window,
        "sell_window": event.sell_window,
        "buy_ok": event.buy_ok,
        "confidence": event.confidence,
        "position_pct": event.position_pct,
        "chase_risk": event.chase_risk,
        "prior_a_days_ago": event.prior_a_days_ago,
        "strategy_label": strategy_label(event.strategy),
        **_first_day_fields(event),
    }


_PUSH_OK_EXCLUDE = frozenset(
    {"none", "failed", "unconfigured", "disabled", "rate_limited", "stale", "soft_skip"}
)


@router.get("/deals")
def list_deals(
    days: int = Query(default=7, ge=1, le=365),
    category: str = Query(default="all"),
    tier_pair: str | None = Query(default=None),
    signal_tier: str | None = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=100),
    pushed_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
):
    from app.database import DealEvent, NvdaSignalEvent
    from app.deal_monitor.content_filter import should_hide_deal_event

    since = now_beijing() - timedelta(days=days)
    if category not in ("all", FEED_AI, FEED_NVDA):
        raise HTTPException(status_code=400, detail="category 支持: all, ai_cooperation, nvda_signal")

    def _query(db: Session):
        rows: list[dict] = []

        if category in ("all", FEED_AI):
            q = db.query(DealEvent).filter(DealEvent.published_at >= since)
            if tier_pair:
                q = q.filter(DealEvent.tier_pair == tier_pair)
            if min_score:
                q = q.filter(DealEvent.materiality_score >= min_score)
            if pushed_only:
                q = q.filter(
                    DealEvent.pushed_at.isnot(None),
                    DealEvent.push_channel.isnot(None),
                    ~DealEvent.push_channel.in_(list(_PUSH_OK_EXCLUDE)),
                )
            for r in q.order_by(desc(DealEvent.published_at)).limit(limit).all():
                if is_test_source_url(r.source_url) or should_hide_deal_event(r):
                    continue
                rows.append(_deal_to_dict(r))

        if category in ("all", FEED_NVDA):
            q = db.query(NvdaSignalEvent).filter(NvdaSignalEvent.published_at >= since)
            if signal_tier:
                q = q.filter(NvdaSignalEvent.signal_tier == signal_tier.upper())
            if min_score:
                q = q.filter(NvdaSignalEvent.materiality_score >= min_score)
            if pushed_only:
                q = q.filter(
                    NvdaSignalEvent.pushed_at.isnot(None),
                    NvdaSignalEvent.push_channel.isnot(None),
                    ~NvdaSignalEvent.push_channel.in_(list(_PUSH_OK_EXCLUDE)),
                )
            for r in q.order_by(desc(NvdaSignalEvent.published_at)).limit(limit).all():
                if is_test_source_url(r.source_url):
                    continue
                rows.append(_nvda_to_dict(r))

        rows.sort(key=lambda x: x.get("published_at") or "", reverse=True)
        return rows[:limit]

    return run_with_db_retry(_query)


@router.get("/deals/stats")
def deals_stats(
    days: int = Query(default=7, ge=1, le=365),
    category: str = Query(default="all"),
):
    from app.database import DealEvent, NvdaSignalEvent
    from app.deal_monitor.content_filter import should_hide_deal_event

    since = now_beijing() - timedelta(days=days)

    def _query(db: Session):
        ai_total = ai_pushed = 0
        nvda_total = nvda_pushed = 0
        ai_anom = nvda_anom = 0
        ai_fd_checked = nvda_fd_checked = 0
        by_tier_pair: dict[str, int] = {}

        if category in ("all", FEED_AI):
            ai_rows = [
                e for e in db.query(DealEvent).filter(DealEvent.published_at >= since).all()
                if not is_test_source_url(e.source_url) and not should_hide_deal_event(e)
            ]
            ai_total = len(ai_rows)
            ai_pushed = len([
                e for e in ai_rows
                if e.pushed_at and e.push_channel and e.push_channel not in _PUSH_OK_EXCLUDE
            ])
            ai_anom = len([e for e in ai_rows if e.first_day_anomaly])
            ai_fd_checked = len([e for e in ai_rows if e.first_day_checked_at])
            by_tier_pair = {}
            for e in ai_rows:
                by_tier_pair[e.tier_pair] = by_tier_pair.get(e.tier_pair, 0) + 1

        if category in ("all", FEED_NVDA):
            nvda_rows = (
                db.query(NvdaSignalEvent)
                .filter(NvdaSignalEvent.published_at >= since)
                .all()
            )
            nvda_rows = [e for e in nvda_rows if not is_test_source_url(e.source_url)]
            nvda_total = len(nvda_rows)
            nvda_pushed = len([
                e for e in nvda_rows
                if e.pushed_at and e.push_channel
                and e.push_channel not in _PUSH_OK_EXCLUDE
            ])
            nvda_anom = len([e for e in nvda_rows if e.first_day_anomaly])
            nvda_fd_checked = len([e for e in nvda_rows if e.first_day_checked_at])

        return {
            "days": days,
            "category": category,
            "total": ai_total + nvda_total,
            "pushed": ai_pushed + nvda_pushed,
            "ai_cooperation_total": ai_total,
            "ai_cooperation_pushed": ai_pushed,
            "nvda_signal_total": nvda_total,
            "nvda_signal_pushed": nvda_pushed,
            "by_tier_pair": by_tier_pair,
            "first_day_anomalies": ai_anom + nvda_anom,
            "first_day_checked": ai_fd_checked + nvda_fd_checked,
        }

    return run_with_db_retry(_query)


@router.get("/deals/{deal_id}")
def get_deal(deal_id: int, category: str = Query(default=FEED_AI)):
    from app.database import DealEvent, NvdaSignalEvent
    from app.deal_monitor.content_filter import should_hide_deal_event

    def _query(db: Session):
        if category == FEED_NVDA:
            event = db.query(NvdaSignalEvent).filter(NvdaSignalEvent.id == deal_id).first()
            if not event:
                raise HTTPException(status_code=404, detail="未找到")
            return _nvda_to_dict(event)
        event = db.query(DealEvent).filter(DealEvent.id == deal_id).first()
        if not event or should_hide_deal_event(event):
            raise HTTPException(status_code=404, detail="未找到")
        return _deal_to_dict(event)

    return run_with_db_retry(_query)


@router.post("/deals/run")
async def run_deals(token: str = Query(default="")):
    import asyncio

    from app.config import DEAL_ADMIN_TOKEN
    from app.deal_monitor.first_day import run_first_day_check
    from app.deal_monitor.pipeline import run_pipeline as run_deal_pipeline
    from app.nvda_signal.pipeline import run_pipeline as run_nvda_pipeline

    if DEAL_ADMIN_TOKEN and token != DEAL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效 token")
    deal_result, nvda_result = await asyncio.gather(run_deal_pipeline(), run_nvda_pipeline())
    first_day = await run_first_day_check(lookback_days=90)
    return {
        **deal_result,
        "nvda_signal": nvda_result,
        "first_day": first_day,
        "saved": (deal_result.get("saved") or 0) + (nvda_result.get("saved") or 0),
    }


@router.post("/deals/first-day-check")
async def deals_first_day_check(
    token: str = Query(default=""),
    days: int = Query(default=90, ge=1, le=365),
    force: bool = Query(default=False),
):
    """手动回填受益方首日涨跌档位。"""
    from app.config import DEAL_ADMIN_TOKEN
    from app.deal_monitor.first_day import run_first_day_check

    if DEAL_ADMIN_TOKEN and token != DEAL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效 token")
    return await run_first_day_check(lookback_days=days, force=force)


# ── 小公司财报日历 ──


@router.get("/earnings")
def list_earnings(
    days: int = Query(default=90, ge=1, le=180),
    history_days: int = Query(default=90, ge=0, le=365),
    include_history: bool = Query(default=True),
    min_score: int = Query(default=0, ge=0, le=100),
    sector: str | None = Query(default=None),
    push_eligible: bool | None = Query(default=None),
    status: str = Query(default="upcoming"),
    limit: int = Query(default=200, ge=1, le=500),
):
    from datetime import timedelta

    from app.database import EarningsEvent
    from app.earnings_monitor.pipeline import days_to_earnings, event_to_dict
    from app.earnings_monitor.trade_window import is_release_past_bj, today_bj

    # 全站以北京时间为主：待发/历史按「财报揭晓北京时刻」是否已过切分
    today = today_bj()
    until = today + timedelta(days=days)
    history_from = today - timedelta(days=history_days) if history_days else today
    # earnings_date 为美股日历日，边界多取 1 天再按北京揭晓时刻细分
    fetch_from = history_from - timedelta(days=1)
    fetch_until = until + timedelta(days=1)

    def _apply_filters(q):
        if sector:
            q = q.filter(EarningsEvent.sector == sector.upper())
        if push_eligible is not None:
            q = q.filter(EarningsEvent.push_eligible.is_(push_eligible))
        if min_score:
            q = q.filter(
                or_(
                    EarningsEvent.score_total.is_(None),
                    EarningsEvent.score_total >= min_score,
                )
            )
        return q

    def _sort_upcoming(rows: list[dict]) -> list[dict]:
        rows.sort(
            key=lambda x: (
                x.get("earnings_date") or "",
                -(x.get("score_total") if x.get("score_total") is not None else -1),
            )
        )
        return rows

    def _sort_history(rows: list[dict]) -> list[dict]:
        rows.sort(
            key=lambda x: -(
                x.get("score_total") if x.get("score_total") is not None else -1
            )
        )
        rows.sort(key=lambda x: x.get("earnings_date") or "", reverse=True)
        return rows

    def _query(db: Session):
        q = db.query(EarningsEvent).filter(
            EarningsEvent.earnings_date >= fetch_from,
            EarningsEvent.earnings_date <= fetch_until,
        )
        q = _apply_filters(q)
        all_rows = q.order_by(
            EarningsEvent.earnings_date.asc(), EarningsEvent.score_total.desc()
        ).limit(limit * 2).all()

        upcoming: list[dict] = []
        history: list[dict] = []
        for r in all_rows:
            past = is_release_past_bj(r.earnings_date, r.session or "TBD")
            d_to = days_to_earnings(r.earnings_date, today, r.session or "TBD")
            if not past:
                if d_to > days:
                    continue
                if status == "upcoming" and r.status not in ("upcoming", "pushed"):
                    continue
                if status and status != "upcoming" and r.status != status:
                    continue
                upcoming.append(event_to_dict(r))
            elif include_history and history_days > 0:
                if d_to < -history_days:
                    continue
                if r.status not in ("archived", "pushed", "upcoming", "rescheduled"):
                    continue
                history.append(event_to_dict(r))

        return {
            "upcoming": _sort_upcoming(upcoming)[:limit],
            "history": _sort_history(history)[:limit],
        }

    return run_with_db_retry(_query)


@router.get("/earnings/stats")
def earnings_stats(
    days: int = Query(default=90, ge=1, le=180),
    history_days: int = Query(default=90, ge=0, le=365),
):
    from datetime import timedelta

    from app.database import EarningsEvent, EarningsPushBatch
    from app.earnings_monitor.config import EARNINGS_PUSH_DAYS_BEFORE, EARNINGS_PUSH_MIN_SCORE
    from app.earnings_monitor.pipeline import days_to_earnings
    from app.earnings_monitor.trade_window import is_release_past_bj, today_bj

    today = today_bj()
    until = today + timedelta(days=days)
    history_from = today - timedelta(days=history_days) if history_days else today
    fetch_from = history_from - timedelta(days=1)
    fetch_until = until + timedelta(days=1)

    def _query(db: Session):
        rows = (
            db.query(EarningsEvent)
            .filter(
                EarningsEvent.earnings_date >= fetch_from,
                EarningsEvent.earnings_date <= fetch_until,
                EarningsEvent.status.in_(
                    ["upcoming", "pushed", "archived", "rescheduled"]
                ),
            )
            .all()
        )
        upcoming_rows = []
        history_n = 0
        for e in rows:
            past = is_release_past_bj(e.earnings_date, e.session or "TBD")
            d_to = days_to_earnings(e.earnings_date, today, e.session or "TBD")
            if not past:
                if d_to > days:
                    continue
                if e.status not in ("upcoming", "pushed"):
                    continue
                upcoming_rows.append(e)
            elif history_days > 0 and d_to >= -history_days:
                history_n += 1

        upcoming = len(upcoming_rows)
        t2 = sum(
            1
            for e in upcoming_rows
            if days_to_earnings(e.earnings_date, today, e.session or "TBD")
            == EARNINGS_PUSH_DAYS_BEFORE
            and e.push_eligible
            and (e.score_total or 0) >= EARNINGS_PUSH_MIN_SCORE
        )
        pushed = sum(
            1
            for e in upcoming_rows
            if e.pushed_at
            and e.push_channel
            not in {
                "failed",
                "unconfigured",
                "too_early",
                "eliminated",
                "below_score",
                "disabled",
            }
        )
        batches = (
            db.query(EarningsPushBatch)
            .filter(
                EarningsPushBatch.pushed_at >= now_beijing() - timedelta(days=30),
                EarningsPushBatch.success.is_(True),
            )
            .count()
        )
        anomalies = (
            db.query(EarningsEvent)
            .filter(
                EarningsEvent.earnings_date >= fetch_from,
                EarningsEvent.earnings_date <= fetch_until,
                EarningsEvent.outcome_anomaly.isnot(None),
                EarningsEvent.outcome_anomaly != "",
            )
            .count()
        )
        return {
            "days": days,
            "history_days": history_days,
            "upcoming": upcoming,
            "history": history_n,
            "t2_due": t2,
            "pushed": pushed,
            "batches_30d": batches,
            "anomalies": anomalies,
        }

    return run_with_db_retry(_query)


@router.get("/earnings/anomalies")
def list_earnings_anomalies(
    days: int = Query(default=14, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=200),
):
    """评分 vs 财报后涨跌对不上的异常列表（网站异常区）。"""
    from datetime import timedelta

    from app.database import EarningsEvent
    from app.earnings_monitor.outcome import anomaly_to_dict
    from app.earnings_monitor.trade_window import today_bj

    today = today_bj()
    since = today - timedelta(days=days + 1)

    def _query(db: Session):
        rows = (
            db.query(EarningsEvent)
            .filter(
                EarningsEvent.earnings_date >= since,
                EarningsEvent.outcome_anomaly.isnot(None),
                EarningsEvent.outcome_anomaly != "",
            )
            .order_by(EarningsEvent.earnings_date.desc(), EarningsEvent.ticker.asc())
            .limit(limit)
            .all()
        )
        return {
            "days": days,
            "count": len(rows),
            "items": [anomaly_to_dict(e) for e in rows],
        }

    return run_with_db_retry(_query)


@router.post("/earnings/outcome-check")
async def earnings_outcome_check(token: str = Query(default="")):
    """手动触发：回填已发财报涨跌并刷新异常标记。"""
    from app.config import DEAL_ADMIN_TOKEN
    from app.earnings_monitor.outcome import run_outcome_check

    if DEAL_ADMIN_TOKEN and token != DEAL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效 token")
    return await run_outcome_check()


@router.get("/earnings/{event_id}")
def get_earnings(event_id: int):
    from app.database import EarningsEvent
    from app.earnings_monitor.pipeline import event_to_dict

    def _query(db: Session):
        event = db.query(EarningsEvent).filter(EarningsEvent.id == event_id).first()
        if not event:
            raise HTTPException(status_code=404, detail="未找到")
        return event_to_dict(event)

    return run_with_db_retry(_query)


@router.post("/earnings/run")
async def run_earnings(token: str = Query(default="")):
    from app.config import DEAL_ADMIN_TOKEN
    from app.earnings_monitor.pipeline import run_calendar_refresh

    if DEAL_ADMIN_TOKEN and token != DEAL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效 token")
    return await run_calendar_refresh()


@router.post("/earnings/push-check")
async def earnings_push_check(token: str = Query(default="")):
    from app.config import DEAL_ADMIN_TOKEN
    from app.earnings_monitor.pipeline import run_t2_push_check

    if DEAL_ADMIN_TOKEN and token != DEAL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效 token")
    return await run_t2_push_check()


# ── AI 主线（子板块相对强弱）──


@router.get("/ai-mainline")
async def ai_mainline_current(force: bool = Query(default=False)):
    from app.ai_mainline.pipeline import compute_mainline

    return await compute_mainline(force=force)


@router.get("/ai-mainline/history")
def ai_mainline_history(days: int = Query(default=30, ge=1, le=120)):
    from app.ai_mainline.pipeline import history_primary

    return {"days": days, "items": history_primary(days)}


@router.post("/ai-mainline/run")
async def ai_mainline_run(token: str = Query(default="")):
    from app.ai_mainline.pipeline import run_ai_mainline_daily
    from app.config import DEAL_ADMIN_TOKEN

    if DEAL_ADMIN_TOKEN and token != DEAL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效 token")
    return await run_ai_mainline_daily(force=True)
