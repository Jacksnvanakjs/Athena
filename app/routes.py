from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.config import CHANGE_HIGHLIGHT_DAYS, FUNDS_SOURCE_FILE, SCRAPE_SECRET, SCRAPE_TIMES, TIMEZONE, USE_TURSO
from app.database import Fund, QuotaRecord, run_with_db_retry
from app.service import run_scrape_and_notify
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
    from app.config import DEAL_POLL_INTERVAL_MIN, ENABLE_SCHEDULER
    from app.database import DealEvent

    def _deal_count(db: Session):
        return db.query(DealEvent).count()

    deal_total = run_with_db_retry(_deal_count)
    return {
        "is_trading_day": is_trading_day(),
        "scrape_times": [f"{h:02d}:{m:02d}" for h, m in SCRAPE_TIMES],
        "timezone": TIMEZONE,
        "database": "turso" if USE_TURSO else "sqlite",
        "now": now_beijing().isoformat(timespec="seconds"),
        "scheduler_enabled": ENABLE_SCHEDULER,
        "deal_monitor": {
            "poll_interval_min": DEAL_POLL_INTERVAL_MIN,
            "events_total": deal_total,
        },
    }


@router.get("/heatmap")
async def heatmap_data(force: bool = Query(default=False)):
    """美股板块/个股热力图数据（缓存约 3 分钟）"""
    from app.heatmap import get_heatmap_data

    return await get_heatmap_data(force=force)


@router.get("/heatmap/stats")
def heatmap_stats(period: str = Query(default="1w")):
    """多周期资金变化统计：1d / 1w / 15d / 1m / 2m / 3m"""
    from app.heatmap import PERIODS, get_period_stats

    if period not in PERIODS:
        raise HTTPException(status_code=400, detail=f"period 支持: {', '.join(PERIODS)}")
    return get_period_stats(period)


@router.post("/heatmap/snapshot")
async def heatmap_snapshot(force: bool = Query(default=True)):
    """手动保存今日美股热力图快照（用于补数据或本地测试）"""
    from app.heatmap import save_daily_snapshot

    return await save_daily_snapshot(force=force)


# ── AI 合作快讯 ──


def _deal_to_dict(event) -> dict:
    return {
        "id": event.id,
        "published_at": event.published_at.isoformat() if event.published_at else None,
        "fetched_at": event.fetched_at.isoformat() if event.fetched_at else None,
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
        "push_channel": event.push_channel,
    }


@router.get("/deals")
def list_deals(
    days: int = Query(default=7, ge=1, le=90),
    tier_pair: str | None = Query(default=None),
    min_score: int = Query(default=0, ge=0, le=100),
    pushed_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
):
    from sqlalchemy import desc

    from app.database import DealEvent
    from app.utils import now_beijing

    since = now_beijing() - timedelta(days=days)

    def _query(db: Session):
        q = db.query(DealEvent).filter(DealEvent.published_at >= since)
        if tier_pair:
            q = q.filter(DealEvent.tier_pair == tier_pair)
        if min_score:
            q = q.filter(DealEvent.materiality_score >= min_score)
        if pushed_only:
            q = q.filter(DealEvent.pushed_at.isnot(None))
        rows = q.order_by(desc(DealEvent.published_at)).limit(limit).all()
        return [_deal_to_dict(r) for r in rows]

    return run_with_db_retry(_query)


@router.get("/deals/stats")
def deals_stats(days: int = Query(default=7, ge=1, le=90)):
    from sqlalchemy import func

    from app.database import DealEvent
    from app.utils import now_beijing

    since = now_beijing() - timedelta(days=days)

    def _query(db: Session):
        total = db.query(DealEvent).filter(DealEvent.published_at >= since).count()
        pushed = (
            db.query(DealEvent)
            .filter(DealEvent.published_at >= since, DealEvent.pushed_at.isnot(None))
            .count()
        )
        pairs = (
            db.query(DealEvent.tier_pair, func.count(DealEvent.id))
            .filter(DealEvent.published_at >= since)
            .group_by(DealEvent.tier_pair)
            .all()
        )
        return {
            "days": days,
            "total": total,
            "pushed": pushed,
            "by_tier_pair": {p: c for p, c in pairs},
        }

    return run_with_db_retry(_query)


@router.get("/deals/{deal_id}")
def get_deal(deal_id: int):
    from app.database import DealEvent

    def _query(db: Session):
        event = db.query(DealEvent).filter(DealEvent.id == deal_id).first()
        if not event:
            raise HTTPException(status_code=404, detail="未找到")
        return _deal_to_dict(event)

    return run_with_db_retry(_query)


@router.post("/deals/run")
async def run_deals(token: str = Query(default="")):
    from app.config import DEAL_ADMIN_TOKEN
    from app.deal_monitor.pipeline import run_pipeline

    if DEAL_ADMIN_TOKEN and token != DEAL_ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="无效 token")
    return await run_pipeline()
