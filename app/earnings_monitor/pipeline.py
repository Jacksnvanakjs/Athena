"""抓日历 → 打分 → 算窗口 → 入库 →（条件）T-2 合并推送。"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.database import EarningsEvent, EarningsPushBatch, db_session
from app.deal_monitor.market_cap import fetch_market_cap, get_cached_market_cap, save_market_cap_cache
from app.deal_monitor.tiers import classify_tier
from app.earnings_monitor.calendar_fetch import fetch_calendar_for_universe
from app.earnings_monitor.config import (
    EARNINGS_LOOKAHEAD_DAYS,
    EARNINGS_MONITOR_ENABLED,
    EARNINGS_PUSH_ALLOW_T_DAY,
    EARNINGS_PUSH_ALLOW_T_MINUS_1,
    EARNINGS_PUSH_DAYS_BEFORE,
    EARNINGS_PUSH_ENABLED,
    EARNINGS_PUSH_MIN_SCORE,
    EARNINGS_SCORE_LOOKAHEAD_DAYS,
    EARNINGS_WEB_MIN_SCORE,
    SECTOR_LABELS,
)
from app.earnings_monitor.push import build_earnings_batch_push
from app.earnings_monitor.scoring import (
    fetch_price_setup_features,
    hard_eliminate,
    score_candidate,
)
from app.earnings_monitor.trade_window import (
    compute_trade_window,
    earnings_release_dt_bj,
    is_release_past_bj,
    pre_earnings_price_as_of,
    relative_release_label_bj,
    session_label,
    today_bj,
)
from app.earnings_monitor.universe import filter_by_market_cap, load_universe
from app.notifier import notify
from app.utils import now_beijing

logger = logging.getLogger(__name__)

_PUSH_FAIL = frozenset({"failed", "unconfigured", "none", ""})


def unique_key(ticker: str, earnings_date: date) -> str:
    return f"{ticker.upper()}:{earnings_date.isoformat()}"


def days_to_earnings(
    earnings_date: date,
    today: date | None = None,
    session: str = "TBD",
) -> int:
    """距财报揭晓（北京时间日历日差）。"""
    rel = earnings_release_dt_bj(earnings_date, session)
    today = today or today_bj()
    return (rel.date() - today).days


def _localize_one_liner(text: str | None) -> str:
    """把历史 one_liner 里的 AI_SEC 等代码换成中文。"""
    if not text:
        return ""
    out = text
    for code, label in SECTOR_LABELS.items():
        out = out.replace(code, label)
    return out


def push_status_for(event: EarningsEvent, today: date | None = None) -> str:
    today = today or today_bj()
    session = event.session or "TBD"
    if event.status == "archived" or is_release_past_bj(event.earnings_date, session):
        return "archived"
    if event.eliminate_reason:
        return "skipped"
    if event.pushed_at and event.push_channel and event.push_channel not in _PUSH_FAIL | {
        "too_early",
        "below_score",
        "eliminated",
        "disabled",
    }:
        return "pushed"
    d = days_to_earnings(event.earnings_date, today, session)
    if d == EARNINGS_PUSH_DAYS_BEFORE and event.push_eligible:
        return "due_today"
    if d > EARNINGS_PUSH_DAYS_BEFORE:
        return "too_early"
    if d == 1 and EARNINGS_PUSH_ALLOW_T_MINUS_1 and event.push_eligible:
        return "due_today"
    if d == 0 and EARNINGS_PUSH_ALLOW_T_DAY and event.push_eligible:
        return "due_today"
    return "skipped"


async def _caps_for(db: Session, tickers: list[str]) -> dict[str, float | None]:
    caps: dict[str, float | None] = {}
    for t in tickers:
        cached = get_cached_market_cap(db, t, max_age_hours=48)
        if cached is not None:
            caps[t] = cached
            continue
        cap = await fetch_market_cap(t)
        caps[t] = cap
        if cap is not None:
            tier = classify_tier(cap, ticker=t)
            save_market_cap_cache(db, t, cap, tier)
    return caps


def _upsert_event(
    db: Session,
    *,
    ticker: str,
    company_name: str,
    sector: str,
    tier: str,
    market_cap_usd: float | None,
    earnings_date: date,
    session: str,
    confirmed: bool,
    source: str,
) -> EarningsEvent:
    key = unique_key(ticker, earnings_date)
    event = db.query(EarningsEvent).filter(EarningsEvent.unique_key == key).first()
    tw = compute_trade_window(earnings_date, session)

    # 同 ticker 旧日期未过期但改期：归档旧 upcoming（未过期按北京揭晓时刻）
    old_rows = (
        db.query(EarningsEvent)
        .filter(
            EarningsEvent.ticker == ticker.upper(),
            EarningsEvent.status.in_(["upcoming", "pushed"]),
            EarningsEvent.earnings_date != earnings_date,
        )
        .all()
    )
    for old in old_rows:
        if old.unique_key != key and not is_release_past_bj(
            old.earnings_date, old.session or "TBD"
        ):
            old.status = "rescheduled"
            old.push_channel = old.push_channel or "rescheduled"

    if not event:
        event = EarningsEvent(unique_key=key, ticker=ticker.upper())
        db.add(event)

    event.company_name = company_name
    event.sector = sector
    event.tier = tier
    event.market_cap_usd = market_cap_usd
    event.earnings_date = earnings_date
    event.session = session
    event.confirmed = confirmed
    event.source = source
    event.strategy = tw.strategy
    event.buy_window = tw.buy_window
    event.sell_window = tw.sell_window
    event.sell_deadline = tw.sell_deadline
    event.buy_window_json = tw.buy_window_json
    event.hold_trading_days_max = tw.hold_trading_days_max
    event.fetched_at = now_beijing()
    still_upcoming = not is_release_past_bj(earnings_date, session or "TBD")
    if event.status in (None, "rescheduled", "archived") or still_upcoming:
        if event.status != "pushed":
            event.status = "upcoming"
    return event


async def _apply_score(event: EarningsEvent) -> None:
    from app.deal_monitor.market_cap import fetch_market_cap, save_market_cap_cache

    days = days_to_earnings(event.earnings_date, session=event.session or "TBD")
    # 评分前若缺市值，强制多源重拉，避免误打 E2
    if event.market_cap_usd is None or event.tier in (None, "", "UNKNOWN"):
        cap = await fetch_market_cap(event.ticker)
        if cap is not None:
            event.market_cap_usd = cap
            event.tier = classify_tier(cap, ticker=event.ticker)

    feats = None
    if days <= EARNINGS_SCORE_LOOKAHEAD_DAYS:
        feats = await fetch_price_setup_features(event.ticker)
    pre_gain = feats.pre_30d_gain if feats else None
    pre_10d = feats.pre_10d_gain if feats else None

    elim = hard_eliminate(
        tier=event.tier or "UNKNOWN",
        market_cap_usd=event.market_cap_usd,
        pre_30d_gain=pre_gain,
        pre_10d_gain=pre_10d,
    )
    result = score_candidate(
        sector=event.sector,
        tier=event.tier or "UNKNOWN",
        session=event.session,
        confirmed=bool(event.confirmed),
        days_to=days,
        pre_30d_gain=pre_gain,
        eliminate_reason=elim,
        market_cap_usd=event.market_cap_usd,
        pre_10d_gain=pre_10d,
        pre_5d_gain=feats.pre_5d_gain if feats else None,
        down_streak=feats.down_streak if feats else None,
        from_21d_high=feats.from_21d_high if feats else None,
    )
    event.score_total = result.score_total
    event.score_detail_json = result.score_detail_json
    event.eliminate_reason = result.eliminate_reason
    event.push_eligible = bool(result.push_eligible and not result.eliminate_reason)
    event.one_liner = result.one_liner
    event.risk_oneliner = result.risk_oneliner
    event.scored_at = now_beijing()
    if result.eliminate_reason:
        event.push_channel = "eliminated"
    elif days > EARNINGS_PUSH_DAYS_BEFORE:
        event.push_channel = event.push_channel if event.pushed_at else "too_early"


async def rescore_event_pre_earnings(event: EarningsEvent) -> dict:
    """用财报前收盘数据重算市值/涨幅/评分（不含财报后价格）。"""
    from app.deal_monitor.market_cap import fetch_market_cap_as_of

    session = event.session or "TBD"
    as_of = pre_earnings_price_as_of(event.earnings_date, session)
    days = days_to_earnings(event.earnings_date, as_of, session)

    cap = await fetch_market_cap_as_of(event.ticker, as_of)
    if cap is None:
        return {"ok": False, "reason": "no_pre_earnings_cap", "as_of": as_of.isoformat()}

    tier = classify_tier(cap, ticker=event.ticker)
    feats = await fetch_price_setup_features(event.ticker, as_of=as_of)
    pre_gain = feats.pre_30d_gain
    pre_10d = feats.pre_10d_gain
    elim = hard_eliminate(
        tier=tier,
        market_cap_usd=cap,
        pre_30d_gain=pre_gain,
        pre_10d_gain=pre_10d,
    )
    result = score_candidate(
        sector=event.sector,
        tier=tier,
        session=session,
        confirmed=bool(event.confirmed),
        days_to=max(0, days),
        pre_30d_gain=pre_gain,
        eliminate_reason=elim,
        market_cap_usd=cap,
        pre_10d_gain=pre_10d,
        pre_5d_gain=feats.pre_5d_gain,
        down_streak=feats.down_streak,
        from_21d_high=feats.from_21d_high,
    )
    event.market_cap_usd = cap
    event.tier = tier
    event.score_total = result.score_total
    event.score_detail_json = result.score_detail_json
    event.eliminate_reason = result.eliminate_reason
    event.push_eligible = bool(result.push_eligible and not result.eliminate_reason)
    event.one_liner = result.one_liner
    event.risk_oneliner = result.risk_oneliner
    event.scored_at = now_beijing()
    if result.eliminate_reason:
        event.push_channel = "eliminated"
    elif event.pushed_at:
        pass
    else:
        event.push_channel = event.push_channel or "archived"
    return {
        "ok": True,
        "as_of": as_of.isoformat(),
        "market_cap_usd": cap,
        "tier": tier,
        "pre_30d_gain": pre_gain,
        "pre_10d_gain": pre_10d,
        "pre_5d_gain": feats.pre_5d_gain,
        "down_streak": feats.down_streak,
        "from_21d_high": feats.from_21d_high,
        "days_to": days,
        "score_total": result.score_total,
        "eliminate_reason": result.eliminate_reason,
        "one_liner": result.one_liner,
        "push_eligible": event.push_eligible,
    }

async def run_calendar_refresh() -> dict:
    if not EARNINGS_MONITOR_ENABLED:
        return {"skipped": True, "reason": "disabled"}

    universe = load_universe()
    summary = {
        "universe": len(universe),
        "kept": 0,
        "calendar_hits": 0,
        "upserted": 0,
        "scored": 0,
        "archived": 0,
        "outcome": {},
    }
    if not universe:
        return summary

    with db_session() as db:
        caps = await _caps_for(db, [u.ticker for u in universe])
        kept = filter_by_market_cap(universe, caps)
        summary["kept"] = len(kept)
        by_ticker = {u.ticker: (u, tier, cap) for u, tier, cap in kept}

        hits = await fetch_calendar_for_universe(list(by_ticker.keys()))
        summary["calendar_hits"] = len(hits)

        for hit in hits:
            meta = by_ticker.get(hit.ticker)
            if not meta:
                continue
            item, tier, cap = meta
            event = _upsert_event(
                db,
                ticker=hit.ticker,
                company_name=item.name,
                sector=item.sector,
                tier=tier,
                market_cap_usd=cap,
                earnings_date=hit.earnings_date,
                session=hit.session,
                confirmed=hit.confirmed,
                source=hit.source,
            )
            summary["upserted"] += 1
            days = days_to_earnings(hit.earnings_date, session=hit.session)
            if days <= EARNINGS_SCORE_LOOKAHEAD_DAYS or event.score_total is None:
                await _apply_score(event)
                summary["scored"] += 1

        # 归档已过期（按北京揭晓时刻）
        candidates = (
            db.query(EarningsEvent)
            .filter(EarningsEvent.status.in_(["upcoming", "pushed"]))
            .all()
        )
        for e in candidates:
            if is_release_past_bj(e.earnings_date, e.session or "TBD"):
                e.status = "archived"
                summary["archived"] += 1

        # 超前瞻窗口隐藏为 archived（earnings_date 为美股日历日，多留 1 天缓冲）
        far = today_bj() + timedelta(days=EARNINGS_LOOKAHEAD_DAYS + 1)
        for e in db.query(EarningsEvent).filter(
            EarningsEvent.earnings_date > far,
            EarningsEvent.status == "upcoming",
        ).all():
            e.status = "archived"
            summary["archived"] += 1

    # 已揭晓：回填财报后涨跌并标记评分异常（独立事务）
    from app.earnings_monitor.outcome import run_outcome_check

    try:
        summary["outcome"] = await run_outcome_check()
    except Exception:
        logger.exception("earnings outcome check failed")
        summary["outcome"] = {"error": True}

    logger.info("earnings calendar refresh: %s", summary)
    return summary


def _eligible_for_push_today(event: EarningsEvent, today: date) -> bool:
    if not event.push_eligible or event.eliminate_reason:
        return False
    if event.score_total is None or event.score_total < EARNINGS_PUSH_MIN_SCORE:
        return False
    if event.status not in ("upcoming", "pushed"):
        return False
    # 已成功推送
    if (
        event.pushed_at
        and event.push_channel
        and event.push_channel
        not in _PUSH_FAIL
        | {"too_early", "below_score", "eliminated", "disabled", "rescheduled"}
    ):
        return False

    d = days_to_earnings(event.earnings_date, today, event.session or "TBD")
    if d == EARNINGS_PUSH_DAYS_BEFORE:
        return True
    # 补推：仅当曾失败或从未成功
    failed_or_never = (not event.pushed_at) or (event.push_channel in _PUSH_FAIL)
    if d == 1 and EARNINGS_PUSH_ALLOW_T_MINUS_1 and failed_or_never:
        return True
    if d == 0 and EARNINGS_PUSH_ALLOW_T_DAY and failed_or_never:
        return True
    return False


async def run_t2_push_check() -> dict:
    if not EARNINGS_MONITOR_ENABLED:
        return {"skipped": True, "reason": "disabled"}
    if not EARNINGS_PUSH_ENABLED:
        return {"skipped": True, "reason": "push_disabled"}

    today = today_bj()
    summary = {"today_bj": today.isoformat(), "batches": 0, "pushed_events": 0, "skipped": 0}

    with db_session() as db:
        upcoming = (
            db.query(EarningsEvent)
            .filter(
                EarningsEvent.status.in_(["upcoming", "pushed"]),
                EarningsEvent.earnings_date >= today,
                EarningsEvent.earnings_date <= today + timedelta(days=EARNINGS_PUSH_DAYS_BEFORE),
            )
            .all()
        )
        by_date: dict[date, list[EarningsEvent]] = {}
        for e in upcoming:
            if _eligible_for_push_today(e, today):
                by_date.setdefault(e.earnings_date, []).append(e)
            else:
                summary["skipped"] += 1

        for edate, events in sorted(by_date.items()):
            # 终身同日批次成功只一次
            existing = (
                db.query(EarningsPushBatch)
                .filter(
                    EarningsPushBatch.earnings_date == edate,
                    EarningsPushBatch.success.is_(True),
                )
                .first()
            )
            if existing:
                summary["skipped"] += len(events)
                continue

            events.sort(key=lambda x: x.score_total or 0, reverse=True)
            title, content = build_earnings_batch_push(edate, events)
            results = await notify(title, content)
            channels = []
            if results.get("pushplus"):
                channels.append("pushplus")
            if results.get("serverchan"):
                channels.append("serverchan")

            success = bool(channels)
            if not results:
                channel = "unconfigured"
                success = False
            elif success:
                channel = "+".join(channels)
            else:
                channel = "failed"

            batch = EarningsPushBatch(
                earnings_date=edate,
                tickers_csv=",".join(e.ticker for e in events),
                title=title,
                content_html=content,
                pushed_at=now_beijing(),
                push_channel=channel,
                success=success,
            )
            db.add(batch)
            db.flush()

            for e in events:
                e.push_batch_id = batch.id
                e.push_channel = channel
                if success:
                    e.pushed_at = now_beijing()
                    e.status = "pushed"
                    summary["pushed_events"] += 1
            summary["batches"] += 1

    logger.info("earnings T-2 push: %s", summary)
    return summary


async def run_pipeline() -> dict:
    """日历刷新 + T-2 推送检查。"""
    cal = await run_calendar_refresh()
    push = await run_t2_push_check()
    return {"calendar": cal, "push": push}


def event_to_dict(event: EarningsEvent) -> dict:
    today = today_bj()
    session = event.session or "TBD"
    days = days_to_earnings(event.earnings_date, today, session)
    days_label = relative_release_label_bj(event.earnings_date, session)
    score_ok = event.score_total is None or event.score_total >= EARNINGS_WEB_MIN_SCORE
    # 展示用窗口统一重算，保证单时点 + 双时区
    tw = compute_trade_window(event.earnings_date, event.session or "TBD")
    return {
        "id": event.id,
        "category": "earnings",
        "category_label": "AI财报",
        "ticker": event.ticker,
        "company_name": event.company_name,
        "sector": event.sector,
        "sector_label": SECTOR_LABELS.get(event.sector, event.sector),
        "tier": event.tier,
        "market_cap_usd": event.market_cap_usd,
        "earnings_date": event.earnings_date.isoformat() if event.earnings_date else None,
        "earnings_release_bj": tw.earnings_release_bj,
        "earnings_release_et": tw.earnings_release_et,
        "session": event.session,
        "session_label": session_label(event.session),
        "confirmed": bool(event.confirmed),
        "score_total": event.score_total,
        "score_detail_json": event.score_detail_json,
        "eliminate_reason": event.eliminate_reason,
        "push_eligible": bool(event.push_eligible),
        "one_liner": _localize_one_liner(event.one_liner),
        "risk_oneliner": event.risk_oneliner,
        "strategy": event.strategy,
        "buy_window": tw.buy_window,
        "sell_window": tw.sell_window,
        "sell_deadline": tw.sell_deadline,
        "buy_window_bj": tw.buy_window_bj,
        "buy_window_et": tw.buy_window_et,
        "sell_window_bj": tw.sell_window_bj,
        "sell_window_et": tw.sell_window_et,
        "sell_deadline_bj": tw.sell_deadline_bj,
        "sell_deadline_et": tw.sell_deadline_et,
        "buy_window_json": tw.buy_window_json,
        "hold_trading_days_max": tw.hold_trading_days_max,
        "status": event.status,
        "days_to": days,
        "days_to_label": days_label,
        "push_status": push_status_for(event, today),
        "fetched_at": event.fetched_at.isoformat() if event.fetched_at else None,
        "scored_at": event.scored_at.isoformat() if event.scored_at else None,
        "pushed_at": event.pushed_at.isoformat() if event.pushed_at else None,
        "push_channel": event.push_channel,
        "source": event.source,
        "web_visible": score_ok and event.status in ("upcoming", "pushed"),
        "post_er_return": event.post_er_return,
        "post_er_return_pct": None
        if event.post_er_return is None
        else round(event.post_er_return * 100, 2),
        "post_er_sessions": event.post_er_sessions,
        "post_er_as_of": event.post_er_as_of.isoformat() if event.post_er_as_of else None,
        "post_er_source": event.post_er_source,
        "outcome_expected": event.outcome_expected,
        "outcome_anomaly": event.outcome_anomaly,
        "outcome_note": event.outcome_note,
        "outcome_checked_at": event.outcome_checked_at.isoformat()
        if event.outcome_checked_at
        else None,
    }
