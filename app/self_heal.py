"""数据自检与缺失补全：部署打断定时任务后，自动发现缺口并触发对应流程。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import (
    DEAL_POLL_INTERVAL_MIN,
    EARNINGS_MONITOR_ENABLED,
    EARNINGS_OUTCOME_LOOKBACK_DAYS,
    EARNINGS_SCORE_LOOKAHEAD_DAYS,
    SELF_HEAL_ENABLED,
)
from app.utils import now_beijing

logger = logging.getLogger(__name__)

# 防止自检与 cron 重叠时重复狂跑
_LOCK = asyncio.Lock()
_LAST_RUN: dict[str, datetime] = {}
_MIN_GAP = {
    "earnings_outcome": timedelta(minutes=25),
    "deal_first_day": timedelta(minutes=25),
    "deal_rescore": timedelta(hours=6),
    "earnings_calendar": timedelta(minutes=45),
    "deal_poll": timedelta(minutes=max(5, DEAL_POLL_INTERVAL_MIN)),
}


def _cooldown_ok(action: str) -> bool:
    last = _LAST_RUN.get(action)
    if last is None:
        return True
    return now_beijing() - last >= _MIN_GAP.get(action, timedelta(minutes=30))


def _mark_ran(action: str) -> None:
    _LAST_RUN[action] = now_beijing()


def audit_data_gaps() -> dict:
    """只读扫描缺口，不跑补全。"""
    from app.database import (
        DealEvent,
        EarningsEvent,
        NvdaSignalEvent,
        db_session,
        is_turso_stream_error,
        reset_engine,
    )
    from app.earnings_monitor.trade_window import is_release_past_bj, today_bj
    from app.source_url_guard import is_test_source_url

    today = today_bj()
    gaps: dict[str, list] = {
        "earnings_missing_post_er": [],
        "earnings_unscored_near": [],
        "deal_missing_first_day": [],
        "nvda_missing_first_day": [],
    }

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with db_session() as db:
                since_er = today - timedelta(days=EARNINGS_OUTCOME_LOOKBACK_DAYS + 1)
                er_rows = (
                    db.query(EarningsEvent)
                    .filter(EarningsEvent.earnings_date >= since_er)
                    .filter(EarningsEvent.earnings_date <= today + timedelta(days=1))
                    .all()
                )
                for e in er_rows:
                    sess = e.session or "TBD"
                    if not is_release_past_bj(e.earnings_date, sess):
                        if (
                            e.earnings_date
                            <= today + timedelta(days=EARNINGS_SCORE_LOOKAHEAD_DAYS)
                            and e.score_total is None
                            and e.status in ("upcoming", "pushed", "rescheduled")
                        ):
                            gaps["earnings_unscored_near"].append(
                                {
                                    "ticker": e.ticker,
                                    "earnings_date": e.earnings_date.isoformat(),
                                    "session": sess,
                                    "status": e.status,
                                }
                            )
                        continue
                    if e.post_er_return is None:
                        gaps["earnings_missing_post_er"].append(
                            {
                                "id": e.id,
                                "ticker": e.ticker,
                                "earnings_date": e.earnings_date.isoformat(),
                                "session": sess,
                                "status": e.status,
                                "score_total": e.score_total,
                            }
                        )

                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=14)
                deals = (
                    db.query(DealEvent)
                    .filter(DealEvent.published_at >= cutoff)
                    .order_by(DealEvent.id.desc())
                    .limit(200)
                    .all()
                )
                for e in deals:
                    if is_test_source_url(e.source_url):
                        continue
                    if e.first_day_checked_at is None or (
                        e.first_day_return is None
                        and (e.first_day_band or "") in ("", "无行情")
                    ):
                        pub = e.published_at
                        if pub and (now_beijing().replace(tzinfo=None) - pub) < timedelta(
                            hours=6
                        ):
                            continue
                        gaps["deal_missing_first_day"].append(
                            {
                                "id": e.id,
                                "ticker": e.beneficiary_ticker,
                                "published_at": pub.isoformat() if pub else None,
                            }
                        )

                nvdas = (
                    db.query(NvdaSignalEvent)
                    .filter(NvdaSignalEvent.published_at >= cutoff)
                    .order_by(NvdaSignalEvent.id.desc())
                    .limit(100)
                    .all()
                )
                for e in nvdas:
                    if is_test_source_url(e.source_url):
                        continue
                    if e.first_day_checked_at is None or e.first_day_return is None:
                        pub = e.published_at
                        if pub and (now_beijing().replace(tzinfo=None) - pub) < timedelta(
                            hours=6
                        ):
                            continue
                        gaps["nvda_missing_first_day"].append(
                            {
                                "id": e.id,
                                "ticker": e.beneficiary_ticker,
                                "published_at": pub.isoformat() if pub else None,
                            }
                        )
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            if is_turso_stream_error(exc) or "timed out" in str(exc).lower():
                reset_engine()
                continue
            raise

    if last_exc is not None:
        raise last_exc

    return {
        "at": now_beijing().isoformat(timespec="seconds"),
        "counts": {k: len(v) for k, v in gaps.items()},
        "gaps": gaps,
        "needs_heal": any(gaps.values()),
    }


async def run_self_heal(*, force: bool = False) -> dict:
    """扫描缺口并按需启动补全流程（带冷却，避免与定时任务叠跑）。"""
    if not SELF_HEAL_ENABLED and not force:
        return {"skipped": True, "reason": "SELF_HEAL_ENABLED=false"}

    if _LOCK.locked() and not force:
        return {"skipped": True, "reason": "already_running"}

    async with _LOCK:
        audit = audit_data_gaps()
        actions: list[dict] = []
        gaps = audit["gaps"]

        # 1) 财报后涨跌缺失 → outcome
        if gaps["earnings_missing_post_er"] and (
            force or _cooldown_ok("earnings_outcome")
        ):
            if EARNINGS_MONITOR_ENABLED:
                from app.earnings_monitor.outcome import run_outcome_check

                logger.info(
                    "自检补全: earnings outcome，缺失 %d 条（含 %s）",
                    len(gaps["earnings_missing_post_er"]),
                    ",".join(
                        g["ticker"] for g in gaps["earnings_missing_post_er"][:8]
                    ),
                )
                try:
                    result = await run_outcome_check()
                    _mark_ran("earnings_outcome")
                    actions.append(
                        {
                            "action": "earnings_outcome",
                            "ok": True,
                            "missing_before": len(gaps["earnings_missing_post_er"]),
                            "result": result,
                        }
                    )
                except Exception as exc:
                    logger.exception("自检补全 earnings outcome 失败")
                    actions.append(
                        {"action": "earnings_outcome", "ok": False, "error": str(exc)[:200]}
                    )

        # 2) 近端财报无评分 → 日历刷新（含打分）
        if gaps["earnings_unscored_near"] and (
            force or _cooldown_ok("earnings_calendar")
        ):
            if EARNINGS_MONITOR_ENABLED:
                from app.earnings_monitor.pipeline import run_calendar_refresh

                logger.info(
                    "自检补全: earnings calendar，未评分 %d 条",
                    len(gaps["earnings_unscored_near"]),
                )
                try:
                    result = await run_calendar_refresh()
                    _mark_ran("earnings_calendar")
                    actions.append(
                        {
                            "action": "earnings_calendar",
                            "ok": True,
                            "unscored_before": len(gaps["earnings_unscored_near"]),
                            "result": {
                                k: result.get(k)
                                for k in ("upserted", "scored", "kept", "outcome")
                                if isinstance(result, dict)
                            },
                        }
                    )
                except Exception as exc:
                    logger.exception("自检补全 earnings calendar 失败")
                    actions.append(
                        {"action": "earnings_calendar", "ok": False, "error": str(exc)[:200]}
                    )

        # 3) 合作快讯 / NVDA 首日回测缺失
        need_fd = gaps["deal_missing_first_day"] or gaps["nvda_missing_first_day"]
        if need_fd and (force or _cooldown_ok("deal_first_day")):
            from app.deal_monitor.first_day import run_first_day_check

            logger.info(
                "自检补全: first_day，deal=%d nvda=%d",
                len(gaps["deal_missing_first_day"]),
                len(gaps["nvda_missing_first_day"]),
            )
            try:
                result = await run_first_day_check(lookback_days=90, force=False)
                _mark_ran("deal_first_day")
                actions.append(
                    {
                        "action": "deal_first_day",
                        "ok": True,
                        "missing_before": len(need_fd),
                        "result": result,
                    }
                )
            except Exception as exc:
                logger.exception("自检补全 first_day 失败")
                actions.append(
                    {"action": "deal_first_day", "ok": False, "error": str(exc)[:200]}
                )

        # 4) 分数 vs 首日回测分差大 → 按新规则重打分
        if force or _cooldown_ok("deal_rescore"):
            from app.config import DEAL_SCORE_OUTCOME_GAP
            from app.database import DealEvent, db_session
            from app.deal_monitor.content_filter import should_hide_deal_event
            from app.deal_monitor.materiality import is_large_score_outcome_gap
            from app.deal_monitor.rescore import rescore_deal_events
            from app.source_url_guard import is_test_source_url

            large_n = 0
            try:
                with db_session() as db:
                    since = now_beijing() - timedelta(days=120)
                    for e in (
                        db.query(DealEvent)
                        .filter(DealEvent.published_at >= since)
                        .filter(DealEvent.first_day_score.isnot(None))
                        .all()
                    ):
                        if is_test_source_url(e.source_url) or should_hide_deal_event(e):
                            continue
                        if is_large_score_outcome_gap(
                            e.materiality_score,
                            e.first_day_score,
                            threshold=DEAL_SCORE_OUTCOME_GAP,
                        ):
                            large_n += 1
            except Exception as exc:
                logger.warning("自检扫描分差失败: %s", exc)
                large_n = 0

            if large_n > 0 or force:
                logger.info("自检补全: deal_rescore，分差大 %d 条", large_n)
                try:
                    with db_session() as db:
                        result = rescore_deal_events(
                            db,
                            lookback_days=120,
                            gap_only=True,
                            gap_threshold=DEAL_SCORE_OUTCOME_GAP,
                            calibrate=True,
                            dry_run=False,
                            limit=300,
                        )
                    _mark_ran("deal_rescore")
                    actions.append(
                        {
                            "action": "deal_rescore",
                            "ok": True,
                            "large_before": large_n,
                            "result": {
                                k: result[k]
                                for k in (
                                    "considered",
                                    "updated",
                                    "large_gap_before",
                                    "large_gap_after",
                                )
                                if k in result
                            },
                        }
                    )
                except Exception as exc:
                    logger.exception("自检补全 deal_rescore 失败")
                    actions.append(
                        {"action": "deal_rescore", "ok": False, "error": str(exc)[:200]}
                    )

        after = audit_data_gaps() if actions else audit
        summary = {
            "enabled": SELF_HEAL_ENABLED,
            "force": force,
            "audit_before": audit["counts"],
            "audit_after": after["counts"],
            "actions": actions,
            "at": now_beijing().isoformat(timespec="seconds"),
        }
        logger.info(
            "数据自检完成 before=%s after=%s actions=%s",
            audit["counts"],
            after["counts"],
            [a["action"] for a in actions],
        )
        return summary
