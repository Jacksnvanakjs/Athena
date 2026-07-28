from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.config import FUNDS_SOURCE_FILE, SCRAPE_SECRET, SCRAPE_TIMES, TIMEZONE, USE_TURSO
from app.database import Fund, QuotaRecord, get_db
from app.service import run_scrape_and_notify
from app.utils import is_trading_day, now_beijing

router = APIRouter(prefix="/api")


def _check_scrape_secret(secret: str | None):
    if SCRAPE_SECRET and secret != SCRAPE_SECRET:
        raise HTTPException(status_code=403, detail="无效的密钥")


@router.get("/funds")
def list_funds(db: Session = Depends(get_db)):
    funds = db.query(Fund).all()
    latest = {}
    previous = {}
    for fund in funds:
        records = (
            db.query(QuotaRecord)
            .filter(QuotaRecord.fund_code == fund.code)
            .order_by(desc(QuotaRecord.scraped_at))
            .limit(2)
            .all()
        )
        latest[fund.code] = records[0] if records else None
        previous[fund.code] = records[1] if len(records) > 1 else None

    return [
        {
            "code": f.code,
            "name": f.name,
            "url": f.url,
            "status": latest[f.code].status if latest[f.code] else None,
            "quota": latest[f.code].quota if latest[f.code] else None,
            "scraped_at": latest[f.code].scraped_at.isoformat() if latest[f.code] else None,
            "previous_status": previous[f.code].status if previous[f.code] else None,
            "previous_quota": previous[f.code].quota if previous[f.code] else None,
        }
        for f in funds
    ]


@router.get("/history/{fund_code}")
def fund_history(
    fund_code: str,
    days: int = Query(default=30, ge=1, le=730),
    db: Session = Depends(get_db),
):
    since = now_beijing() - timedelta(days=days)
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


@router.get("/history")
def all_history(
    days: int = Query(default=30, ge=1, le=730),
    db: Session = Depends(get_db),
):
    since = now_beijing() - timedelta(days=days)
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
