from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.database import Fund, QuotaRecord, SessionLocal, is_turso_stream_error, reset_engine
from app.notifier import build_change_message, build_collective_change_message, notify
from app.scraper import scrape_all_funds
from app.utils import FundInfo, load_funds, now_beijing


def sync_funds_to_db(db: Session, funds: list[FundInfo]):
    for fund in funds:
        existing = db.query(Fund).filter(Fund.code == fund.code).first()
        if not existing:
            db.add(Fund(code=fund.code, name=fund.name, url=fund.url))
        else:
            existing.name = fund.name
            existing.url = fund.url
    db.commit()


def get_latest_quotas(db: Session) -> dict[str, QuotaRecord]:
    records = {}
    fund_codes = [f.code for f in db.query(Fund).all()]
    for code in fund_codes:
        latest = (
            db.query(QuotaRecord)
            .filter(QuotaRecord.fund_code == code)
            .order_by(desc(QuotaRecord.scraped_at))
            .first()
        )
        if latest:
            records[code] = latest
    return records


async def _run_scrape_with_session(db: Session, funds: list[FundInfo]) -> dict:
    sync_funds_to_db(db, funds)
    previous = get_latest_quotas(db)
    results = await scrape_all_funds(funds)
    now = now_beijing()
    changes = []

    for r in results:
        record = QuotaRecord(
            fund_code=r["code"],
            fund_name=r["name"],
            status=r["status"],
            quota=r["quota"],
            scraped_at=now,
        )
        db.add(record)

        prev = previous.get(r["code"])
        if prev and (prev.quota != r["quota"] or prev.status != r["status"]):
            changes.append(
                {
                    "code": r["code"],
                    "name": r["name"],
                    "old_quota": prev.quota,
                    "new_quota": r["quota"],
                    "old_status": prev.status,
                    "new_status": r["status"],
                }
            )

    db.commit()

    notifications = []
    if changes:
        total = len(funds)
        threshold = total / 3

        if len(changes) >= threshold:
            title = f"⚠️ 额度集体变化 ({len(changes)}/{total})"
            content = build_collective_change_message(changes, total)
            await notify(title, content)
            notifications.append("collective")
        else:
            title = f"基金额度变化 ({len(changes)}支)"
            content = build_change_message(changes)
            await notify(title, content)
            notifications.append("individual")

    return {
        "success": True,
        "scraped": len(results),
        "changes": changes,
        "notifications": notifications,
        "results": results,
    }


async def run_scrape_and_notify(source_file: str) -> dict:
    funds = load_funds(source_file)
    if not funds:
        return {"success": False, "message": "未找到基金数据", "changes": []}

    db = SessionLocal()
    try:
        return await _run_scrape_with_session(db, funds)
    except Exception as exc:
        if is_turso_stream_error(exc):
            reset_engine()
            db.close()
            db = SessionLocal()
            return await _run_scrape_with_session(db, funds)
        raise
    finally:
        db.close()
