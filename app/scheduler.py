import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import ENABLE_SCHEDULER, FUNDS_SOURCE_FILE, SCRAPE_HOURS, TIMEZONE
from app.service import run_scrape_and_notify
from app.utils import is_trading_day

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def scheduled_scrape():
    if not is_trading_day():
        logger.info("非交易日，跳过抓取")
        return
    logger.info("开始定时抓取基金额度...")
    result = await run_scrape_and_notify(FUNDS_SOURCE_FILE)
    logger.info("抓取完成: %s", result)


def start_scheduler():
    if not ENABLE_SCHEDULER:
        logger.info("内置调度器已禁用（ENABLE_SCHEDULER=false），请使用外部 cron 触发 /api/cron/scrape")
        return
    for hour in SCRAPE_HOURS:
        scheduler.add_job(
            scheduled_scrape,
            CronTrigger(hour=hour, minute=0, timezone=TIMEZONE),
            id=f"scrape_{hour}",
            replace_existing=True,
        )
    scheduler.start()
    logger.info("调度器已启动，时区: %s，抓取时间: %s:00", TIMEZONE, SCRAPE_HOURS)


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
