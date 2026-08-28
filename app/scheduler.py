import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import DEAL_POLL_INTERVAL_MIN, ENABLE_SCHEDULER, FUNDS_SOURCE_FILE, SCRAPE_TIMES, TIMEZONE
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
    """美东每个交易日 16:30 保存收盘快照（北京次日凌晨 04:30/05:30）。"""
    if not is_us_trading_day():
        logger.info("非美股交易日，跳过热力图快照")
        return
    from app.heatmap import save_daily_snapshot

    logger.info("开始保存美股热力图每日快照...")
    result = await save_daily_snapshot(force=True)
    logger.info("热力图快照完成: %s", result)


async def scheduled_deal_poll():
    """AI 合作快讯 + 黄仁勋动向 RSS 轮询。"""
    from app.deal_monitor.pipeline import run_pipeline as run_deal_pipeline
    from app.nvda_signal.pipeline import run_pipeline as run_nvda_pipeline

    logger.info("开始 deal_monitor RSS 轮询...")
    deal_result = await run_deal_pipeline()
    logger.info("deal_monitor 完成: %s", deal_result)
    logger.info("开始 nvda_signal 轮询...")
    nvda_result = await run_nvda_pipeline()
    logger.info("nvda_signal 完成: %s", nvda_result)


async def scheduled_deal_market_cap_refresh():
    """每日刷新市值分档缓存。"""
    from app.database import db_session
    from app.deal_monitor.market_cap import refresh_all_seed_market_caps

    logger.info("开始刷新 deal_monitor 市值缓存...")
    with db_session() as db:
        count = await refresh_all_seed_market_caps(db)
    logger.info("市值缓存刷新完成: %d tickers", count)


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
    scheduler.add_job(
        scheduled_deal_poll,
        IntervalTrigger(minutes=DEAL_POLL_INTERVAL_MIN),
        id="deal_monitor_poll",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_deal_market_cap_refresh,
        CronTrigger(hour=17, minute=0, timezone="America/New_York"),
        id="deal_monitor_market_cap",
        replace_existing=True,
    )
    scheduler.start()
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in SCRAPE_TIMES)
    logger.info(
        "调度器已启动，时区: %s，基金抓取: %s；美股热力图快照: 美东 16:30；"
        "deal_monitor: 每 %d 分钟",
        TIMEZONE,
        times,
        DEAL_POLL_INTERVAL_MIN,
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
