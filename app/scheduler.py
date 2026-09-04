import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import (
    AI_MAINLINE_ENABLED,
    DEAL_POLL_INTERVAL_MIN,
    ENABLE_SCHEDULER,
    EARNINGS_CALENDAR_REFRESH_HOURS,
    EARNINGS_MONITOR_ENABLED,
    FUNDS_SOURCE_FILE,
    NVDA_SIGNAL_ENABLED,
    SCRAPE_TIMES,
    SELF_HEAL_ENABLED,
    SELF_HEAL_INTERVAL_MIN,
    TIMEZONE,
)
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
    if AI_MAINLINE_ENABLED:
        from app.ai_mainline.pipeline import run_ai_mainline_daily

        logger.info("开始 AI 主线日快照...")
        ml = await run_ai_mainline_daily(force=True)
        logger.info("AI 主线日快照完成: %s", ml)


async def scheduled_ai_mainline_daily():
    """美东 16:35：AI 主线日快照（heatmap 之后的兜底）。"""
    if not AI_MAINLINE_ENABLED:
        return
    if not is_us_trading_day():
        logger.info("非美股交易日，跳过 AI 主线快照")
        return
    from app.ai_mainline.pipeline import run_ai_mainline_daily

    logger.info("开始 AI 主线日快照（16:35）...")
    result = await run_ai_mainline_daily(force=True)
    logger.info("AI 主线日快照完成: %s", result)


async def scheduled_deal_poll():
    """AI 合作快讯 RSS 轮询。"""
    from app.deal_monitor.pipeline import run_pipeline as run_deal_pipeline

    logger.info("开始 deal_monitor RSS 轮询...")
    deal_result = await run_deal_pipeline()
    logger.info("deal_monitor 完成: %s", deal_result)


async def scheduled_nvda_poll():
    """黄仁勋 / NVDA 动向轮询（与 deal 拆开，缩短各自墙钟）。"""
    from app.nvda_signal.pipeline import run_pipeline as run_nvda_pipeline

    logger.info("开始 nvda_signal 轮询...")
    nvda_result = await run_nvda_pipeline()
    logger.info("nvda_signal 完成: %s", nvda_result)


async def scheduled_deal_first_day():
    """美股收盘后回填合作快讯受益方首日涨跌档位。"""
    from app.deal_monitor.first_day import run_first_day_check

    logger.info("开始 deal 首日股价回测...")
    result = await run_first_day_check(lookback_days=90)
    logger.info("deal 首日回测完成: %s", result)


async def scheduled_deal_market_cap_refresh():
    """每日刷新市值分档缓存。"""
    from app.database import db_session
    from app.deal_monitor.market_cap import refresh_all_seed_market_caps

    logger.info("开始刷新 deal_monitor 市值缓存...")
    with db_session() as db:
        count = await refresh_all_seed_market_caps(db)
    logger.info("市值缓存刷新完成: %d tickers", count)


async def scheduled_earnings_calendar():
    if not EARNINGS_MONITOR_ENABLED:
        return
    from app.earnings_monitor.pipeline import run_calendar_refresh

    logger.info("开始 earnings 日历刷新...")
    result = await run_calendar_refresh()
    logger.info("earnings 日历完成: %s", result)


async def scheduled_earnings_t2_push():
    if not EARNINGS_MONITOR_ENABLED:
        return
    from app.earnings_monitor.pipeline import run_t2_push_check

    logger.info("开始 earnings T-2 推送检查...")
    result = await run_t2_push_check()
    logger.info("earnings T-2 推送完成: %s", result)


async def scheduled_earnings_outcome():
    """美股收盘后回填财报涨跌，对照评分标异常。"""
    if not EARNINGS_MONITOR_ENABLED:
        return
    from app.earnings_monitor.outcome import run_outcome_check

    logger.info("开始 earnings 财报后涨跌对照...")
    result = await run_outcome_check()
    logger.info("earnings 财报后对照完成: %s", result)


async def scheduled_self_heal():
    """扫描数据缺口并自动补跑相关流程（抗部署打断）。"""
    if not SELF_HEAL_ENABLED:
        return
    from app.self_heal import run_self_heal

    logger.info("开始数据自检补全...")
    result = await run_self_heal()
    logger.info("数据自检补全完成: %s", result)


def scheduler_status() -> dict:
    """供 /health 与 /api/status 展示调度器是否在跑。"""
    jobs: list[dict] = []
    if scheduler.running:
        for job in scheduler.get_jobs():
            nxt = job.next_run_time
            jobs.append(
                {
                    "id": job.id,
                    "next_run": nxt.isoformat(timespec="seconds") if nxt else None,
                }
            )
    deal_next = None
    nvda_next = None
    for job in jobs:
        if job["id"] == "deal_monitor_poll":
            deal_next = job["next_run"]
        elif job["id"] == "nvda_signal_poll":
            nvda_next = job["next_run"]
    return {
        "enabled": ENABLE_SCHEDULER,
        "running": scheduler.running,
        "deal_poll_interval_min": DEAL_POLL_INTERVAL_MIN,
        "deal_poll_next_run": deal_next,
        "nvda_poll_next_run": nvda_next,
        "jobs": jobs,
    }


def start_scheduler():
    if not ENABLE_SCHEDULER:
        logger.warning(
            "内置调度器已禁用（ENABLE_SCHEDULER=false）；合作快讯不会自动扫描，"
            "生产环境请设为 true 并保持进程 7×24 运行"
        )
        return
    for hour, minute in SCRAPE_TIMES:
        scheduler.add_job(
            scheduled_scrape,
            CronTrigger(hour=hour, minute=minute, timezone=TIMEZONE),
            id=f"scrape_{hour:02d}_{minute:02d}",
            replace_existing=True,
        )
    scheduler.add_job(
        scheduled_heatmap_snapshot,
        CronTrigger(hour=16, minute=30, timezone="America/New_York"),
        id="heatmap_snapshot_us_close",
        replace_existing=True,
    )
    if AI_MAINLINE_ENABLED:
        scheduler.add_job(
            scheduled_ai_mainline_daily,
            CronTrigger(hour=16, minute=35, timezone="America/New_York"),
            id="ai_mainline_daily_et_1635",
            replace_existing=True,
        )
    scheduler.add_job(
        scheduled_deal_poll,
        IntervalTrigger(minutes=DEAL_POLL_INTERVAL_MIN),
        id="deal_monitor_poll",
        replace_existing=True,
        next_run_time=datetime.now(),
        max_instances=1,
        coalesce=True,
    )
    if NVDA_SIGNAL_ENABLED:
        # 错开约 30s，避免与 deal 同时打满通稿/SEC
        scheduler.add_job(
            scheduled_nvda_poll,
            IntervalTrigger(minutes=DEAL_POLL_INTERVAL_MIN),
            id="nvda_signal_poll",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(seconds=30),
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        scheduled_deal_market_cap_refresh,
        CronTrigger(hour=17, minute=0, timezone="America/New_York"),
        id="deal_monitor_market_cap",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_deal_first_day,
        CronTrigger(hour=16, minute=45, timezone="America/New_York"),
        id="deal_first_day_et_1645",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_deal_first_day,
        CronTrigger(hour=10, minute=15, timezone="America/New_York"),
        id="deal_first_day_et_1015",
        replace_existing=True,
    )
    if EARNINGS_MONITOR_ENABLED:
        scheduler.add_job(
            scheduled_earnings_calendar,
            IntervalTrigger(hours=max(1, EARNINGS_CALENDAR_REFRESH_HOURS)),
            id="earnings_calendar_refresh",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_earnings_calendar,
            CronTrigger(hour=8, minute=0, timezone="America/New_York"),
            id="earnings_calendar_et_0800",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_earnings_calendar,
            CronTrigger(hour=8, minute=30, timezone="America/New_York"),
            id="earnings_score_et_0830",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_earnings_t2_push,
            CronTrigger(hour=9, minute=0, timezone="America/New_York"),
            id="earnings_t2_push_et_0900",
            replace_existing=True,
        )
        # 盘后反应 + 次日开盘后各扫一次，刷新异常区
        scheduler.add_job(
            scheduled_earnings_outcome,
            CronTrigger(hour=20, minute=30, timezone="America/New_York"),
            id="earnings_outcome_et_2030",
            replace_existing=True,
        )
        scheduler.add_job(
            scheduled_earnings_outcome,
            CronTrigger(hour=10, minute=0, timezone="America/New_York"),
            id="earnings_outcome_et_1000",
            replace_existing=True,
        )
    if SELF_HEAL_ENABLED:
        scheduler.add_job(
            scheduled_self_heal,
            IntervalTrigger(minutes=max(10, SELF_HEAL_INTERVAL_MIN)),
            id="data_self_heal",
            replace_existing=True,
            next_run_time=datetime.now() + timedelta(seconds=90),
            max_instances=1,
            coalesce=True,
        )
    scheduler.start()
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in SCRAPE_TIMES)
    logger.info(
        "调度器已启动，时区: %s，基金抓取: %s；美股热力图快照: 美东 16:30；"
        "deal_monitor: 每 %d 分钟；nvda_signal: %s；earnings: %s；ai_mainline: %s；"
        "self_heal: %s",
        TIMEZONE,
        times,
        DEAL_POLL_INTERVAL_MIN,
        "开启" if NVDA_SIGNAL_ENABLED else "关闭",
        "开启" if EARNINGS_MONITOR_ENABLED else "关闭",
        "开启" if AI_MAINLINE_ENABLED else "关闭",
        f"每{SELF_HEAL_INTERVAL_MIN}分钟" if SELF_HEAL_ENABLED else "关闭",
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
