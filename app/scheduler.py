import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import ENABLE_SCHEDULER, FUNDS_SOURCE_FILE, NEWS_MONITOR_INTERVAL_MIN, SCRAPE_TIMES, TIMEZONE
from app.service import run_scrape_and_notify
from app.utils import is_trading_day, is_us_trading_day

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def scheduled_scrape():
    if not is_trading_day():
        logger.info("非交易日，跳过抓取")
        return
    logger.info("开始定时抓取基金额度...")
    result = await run_scrape_and_notify(FUNDS_SOURCE_FILE)
    logger.info("抓取完成: %s", result)


async def scheduled_heatmap_snapshot():
    """美股收盘后（美东 16:30）保存当日热力图快照。"""
    if not is_us_trading_day():
        logger.info("非美股交易日，跳过热力图快照")
        return
    from app.heatmap import save_daily_snapshot

    logger.info("开始保存美股热力图每日快照...")
    result = await save_daily_snapshot(force=True)
    logger.info("热力图快照完成: %s", result)


async def scheduled_news_check():
    """AI产业链合作新闻监控（候选抓取+关键词筛选，重点才推手机）"""
    from app.news_monitor import update_latest

    try:
        result = await update_latest()
        logger.info("AI news monitor: kept=%s pushed=%s", result.get("kept"), result.get("pushed"))
    except Exception as exc:
        logger.exception("AI news monitor failed: %s", exc)


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
    # 美东收盘 16:00 后约 30 分钟落库，覆盖盘后修正
    scheduler.add_job(
        scheduled_heatmap_snapshot,
        CronTrigger(hour=16, minute=30, timezone="America/New_York"),
        id="heatmap_snapshot_us_close",
        replace_existing=True,
    )

    # 新闻监控：尽量快但别过载（分钟维度）
    if NEWS_MONITOR_INTERVAL_MIN > 0:
        scheduler.add_job(
            scheduled_news_check,
            CronTrigger(minute=f"*/{NEWS_MONITOR_INTERVAL_MIN}", timezone=TIMEZONE),
            id="news_ai_monitor",
            replace_existing=True,
        )
    scheduler.start()
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in SCRAPE_TIMES)
    logger.info(
        "调度器已启动，时区: %s，基金抓取: %s；美股热力图快照: 美东 16:30",
        TIMEZONE,
        times,
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
