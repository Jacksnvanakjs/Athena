import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import ENABLE_SCHEDULER, FUNDS_SOURCE_FILE, SCRAPE_TIMES, TIMEZONE
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
    for hour, minute in SCRAPE_TIMES:
        scheduler.add_job(
            scheduled_scrape,
            CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE),
            id=f"scrape_{hour:02d}_{minute:02d}",
            replace_existing=True,
        )
    scheduler.start()
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in SCRAPE_TIMES)
    logger.info("调度器已启动，时区: %s，抓取时间: %s", TIMEZONE, times)


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
