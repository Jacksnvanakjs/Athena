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
    return {
        "is_trading_day": is_trading_day(),
        "scrape_times": [f"{h:02d}:{m:02d}" for h, m in SCRAPE_TIMES],
        "timezone": TIMEZONE,
        "database": "turso" if USE_TURSO else "sqlite",
        "now": now_beijing().isoformat(timespec="seconds"),
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
