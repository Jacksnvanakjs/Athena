"""财报后涨跌 vs 评分对照：自动回填，标出对不上的异常供网站展示与改机制。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

from app.earnings_monitor.config import (
    EARNINGS_OUTCOME_FALSE_NEG_PCT,
    EARNINGS_OUTCOME_FALSE_POS_PCT,
    EARNINGS_OUTCOME_LOOKBACK_DAYS,
    EARNINGS_PUSH_MIN_SCORE,
)
from app.earnings_monitor.trade_window import (
    is_release_past_bj,
    pre_earnings_price_as_of,
    today_bj,
)
from app.utils import now_beijing

logger = logging.getLogger(__name__)

ANOMALY_FALSE_POSITIVE = "false_positive"  # 高分/可推却大跌
ANOMALY_FALSE_NEGATIVE = "false_negative"  # 低分/淘汰却大涨

ANOMALY_LABELS = {
    ANOMALY_FALSE_POSITIVE: "高分却大跌",
    ANOMALY_FALSE_NEGATIVE: "低分/淘汰却大涨",
}

EXPECTED_LABELS = {
    "bullish": "看涨（高分）",
    "bearish": "看弱（低分/淘汰）",
    "skip": "不对照",
}


@dataclass
class PostErMove:
    pre_close: float
    last_price: float
    ret: float
    sessions_after: int
    as_of: date
    source: str


@dataclass
class OutcomeJudgement:
    expected: str  # bullish | bearish | skip
    anomaly: str | None
    note: str


def expected_direction(
    *,
    score_total: int | None,
    push_eligible: bool,
    eliminate_reason: str | None,
) -> str:
    """根据财报前评分推断「应」涨还是弱。T0/无市值不纳入对照。"""
    reason = eliminate_reason or ""
    if reason.startswith("E1:") or reason.startswith("E2:"):
        return "skip"
    if reason:
        return "bearish"
    if score_total is None:
        return "skip"
    if push_eligible or score_total >= EARNINGS_PUSH_MIN_SCORE:
        return "bullish"
    return "bearish"


def judge_anomaly(
    *,
    expected: str,
    post_ret: float | None,
    score_total: int | None = None,
    eliminate_reason: str | None = None,
) -> OutcomeJudgement:
    if expected == "skip" or post_ret is None:
        return OutcomeJudgement(
            expected=expected,
            anomaly=None,
            note="暂无对照（T0/无分/尚无财报后价格）",
        )

    neg_th = EARNINGS_OUTCOME_FALSE_POS_PCT / 100.0
    pos_th = EARNINGS_OUTCOME_FALSE_NEG_PCT / 100.0
    score_txt = "—" if score_total is None else str(score_total)
    elim = eliminate_reason or "—"

    if expected == "bullish" and post_ret <= -neg_th:
        return OutcomeJudgement(
            expected=expected,
            anomaly=ANOMALY_FALSE_POSITIVE,
            note=(
                f"评分{score_txt}≥{EARNINGS_PUSH_MIN_SCORE}看涨，"
                f"财报后却{post_ret * 100:.1f}%≤-{EARNINGS_OUTCOME_FALSE_POS_PCT:g}%"
            ),
        )
    if expected == "bearish" and post_ret >= pos_th:
        return OutcomeJudgement(
            expected=expected,
            anomaly=ANOMALY_FALSE_NEGATIVE,
            note=(
                f"评分{score_txt}/淘汰({elim})看弱，"
                f"财报后却{post_ret * 100:.1f}%≥+{EARNINGS_OUTCOME_FALSE_NEG_PCT:g}%"
            ),
        )
    direction = "涨" if post_ret >= 0 else "跌"
    return OutcomeJudgement(
        expected=expected,
        anomaly=None,
        note=f"评分{score_txt}与财报后{direction}{abs(post_ret) * 100:.1f}%方向一致",
    )


async def fetch_post_er_move(
    ticker: str,
    earnings_date: date,
    session: str,
    *,
    max_sessions: int = 2,
    allow_live: bool = True,
) -> PostErMove | None:
    """财报前收盘 → 财报后最多 max_sessions 个交易日收盘（或盘后/多源现价兜底）。"""
    from app.market_data import fetch_daily_closes

    as_of = pre_earnings_price_as_of(earnings_date, session or "TBD")
    closes = await fetch_daily_closes(ticker, lookback_days=40)
    if not closes:
        return None
    pre = [(d, c) for d, c in closes if d <= as_of and c is not None]
    if not pre:
        return None
    pre_d, pre_c = pre[-1]
    if not pre_c or pre_c <= 0:
        return None

    after = [(d, c) for d, c in closes if d > as_of and c is not None]
    if after:
        use = after[: max(1, max_sessions)]
        last_d, last_c = use[-1]
        return PostErMove(
            pre_close=pre_c,
            last_price=last_c,
            ret=(last_c - pre_c) / pre_c,
            sessions_after=len(use),
            as_of=last_d,
            source=f"daily:{pre_d}→{last_d}",
        )

    if not allow_live:
        return None

    live = await _resolve_live_post_price(ticker, pre_c)
    if not live:
        return None
    return PostErMove(
        pre_close=pre_c,
        last_price=live.price,
        ret=(live.price - pre_c) / pre_c,
        sessions_after=0,
        as_of=live.as_of,
        source=live.source,
    )


@dataclass
class _LivePx:
    price: float
    as_of: date
    source: str


async def _resolve_live_post_price(
    ticker: str,
    pre_close: float,
    *,
    prefer: dict[str, _LivePx] | None = None,
) -> _LivePx | None:
    """主源失败自动换源。有效价须相对财报前收盘有实质变动（≥0.2%）。"""
    if prefer and ticker in prefer:
        cand = prefer[ticker]
        if abs(cand.price / pre_close - 1.0) >= 0.002:
            return cand

    # 1) TickDB/热力图（已解析 post_market_quote）
    try:
        from app.market_data import fetch_quotes

        quotes, src_label = await fetch_quotes([ticker])
        q = quotes.get(ticker) or {}
        px = q.get("price")
        if px is not None and float(px) > 0 and abs(float(px) / pre_close - 1.0) >= 0.002:
            return _LivePx(float(px), today_bj(), f"heatmap:{src_label}")
    except Exception:
        logger.debug("heatmap quotes failed for %s", ticker, exc_info=True)

    # 2) CNBC 盘后/盘前
    cnbc = await _fetch_cnbc_extended(ticker)
    if cnbc and abs(cnbc.price / pre_close - 1.0) >= 0.002:
        return cnbc

    # 3) Yahoo 盘后 K（易 429）
    ah = await _fetch_yahoo_extended_last(ticker)
    if ah and abs(ah.price / pre_close - 1.0) >= 0.002:
        return ah

    # 4) Finnhub
    fh = await _fetch_finnhub_quote(ticker)
    if fh and abs(fh.price / pre_close - 1.0) >= 0.002:
        return fh

    return None


_YAHOO_AH_CACHE: dict[str, tuple[float, _LivePx]] = {}
_YAHOO_AH_CACHE_TTL = 600.0  # seconds


async def _fetch_yahoo_extended_last(ticker: str) -> _LivePx | None:
    """用 includePrePost K 线取常规盘结束后最后一笔。优先 query2 5m，抗限流。"""
    import asyncio
    import time
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import httpx

    from app.heatmap import HEADERS, _yahoo_symbol

    now = time.time()
    cached = _YAHOO_AH_CACHE.get(ticker.upper())
    if cached and now - cached[0] < _YAHOO_AH_CACHE_TTL:
        return cached[1]

    et = ZoneInfo("America/New_York")
    ysym = _yahoo_symbol(ticker)
    headers = {**HEADERS, "Referer": "https://finance.yahoo.com/"}
    urls = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ysym}",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
    )
    params = {"interval": "5m", "range": "5d", "includePrePost": "true"}

    def _parse(payload: dict) -> _LivePx | None:
        result = ((payload.get("chart") or {}).get("result")) or []
        if not result:
            return None
        meta = result[0].get("meta") or {}
        for key, src in (
            ("postMarketPrice", "yahoo_post_meta"),
            ("preMarketPrice", "yahoo_pre_meta"),
        ):
            px = meta.get(key)
            if px is not None and float(px) > 0:
                return _LivePx(float(px), today_bj(), src)

        ts = result[0].get("timestamp") or []
        closes = (
            ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")
            or []
        )
        reg_ts = meta.get("regularMarketTime")
        last: tuple[datetime, float] | None = None
        for tstamp, cl in zip(ts, closes):
            if cl is None:
                continue
            if reg_ts is not None and int(tstamp) <= int(reg_ts):
                continue
            last = (datetime.fromtimestamp(int(tstamp), tz=et), float(cl))
        if last is None:
            return None
        dt, px = last
        return _LivePx(px, dt.date(), f"yahoo_ah_5m:{dt.strftime('%Y-%m-%d %H:%M')}")

    try:
        async with httpx.AsyncClient(
            headers=headers, timeout=25, follow_redirects=True
        ) as client:
            for url in urls:
                resp = await client.get(url, params=params)
                if resp.status_code == 429:
                    await asyncio.sleep(3)
                    resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    continue
                live = _parse(resp.json() or {})
                if live:
                    _YAHOO_AH_CACHE[ticker.upper()] = (now, live)
                    return live
    except Exception:
        logger.debug("yahoo extended last failed for %s", ticker, exc_info=True)
    return None


async def _fetch_cnbc_extended(ticker: str) -> _LivePx | None:
    """CNBC ExtendedMktQuote（盘后/盘前），Yahoo 限流时的稳定兜底。"""
    import re

    import httpx

    url = "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
    params = {
        "symbols": ticker,
        "requestMethod": "quick",
        "exthrs": "1",
        "noform": "1",
        "partnerId": "2",
        "fund": "1",
        "output": "json",
        "events": "1",
    }
    try:
        async with httpx.AsyncClient(
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return None
            quotes = (
                (resp.json() or {}).get("FormattedQuoteResult") or {}
            ).get("FormattedQuote") or []
            if not quotes:
                return None
            q = quotes[0] or {}
            ext = q.get("ExtendedMktQuote") or {}
            last = ext.get("last") or ext.get("Last")
            if not last:
                return None
            # "$376.60" / "376.60" / "+23.14%"
            text = str(last).replace(",", "").replace("$", "").strip()
            px = float(re.sub(r"[^0-9.\-]", "", text) or 0)
            if px <= 0:
                return None
            as_of = today_bj()
            timedate = str(ext.get("last_timedate") or ext.get("last_time") or "")
            # "09/02/26 EDT" → date
            m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", timedate)
            if m:
                mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if yy < 100:
                    yy += 2000
                try:
                    as_of = date(yy, mm, dd)
                except ValueError:
                    pass
            kind = str(ext.get("type") or "EXT").lower()
            return _LivePx(px, as_of, f"cnbc_{kind}")
    except Exception:
        logger.debug("cnbc extended failed for %s", ticker, exc_info=True)
    return None


async def _fetch_finnhub_quote(ticker: str) -> _LivePx | None:
    import os

    import httpx

    key = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://finnhub.io/api/v1/quote",
                params={"symbol": ticker, "token": key},
            )
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
            px = data.get("c")
            if px is None or float(px) <= 0:
                return None
            return _LivePx(float(px), today_bj(), "finnhub")
    except Exception:
        logger.debug("finnhub quote failed for %s", ticker, exc_info=True)
        return None


def apply_outcome_to_event(event, move: PostErMove | None) -> OutcomeJudgement:
    expected = expected_direction(
        score_total=event.score_total,
        push_eligible=bool(event.push_eligible),
        eliminate_reason=event.eliminate_reason,
    )
    post_ret = move.ret if move else None
    judgement = judge_anomaly(
        expected=expected,
        post_ret=post_ret,
        score_total=event.score_total,
        eliminate_reason=event.eliminate_reason,
    )
    if move is not None:
        event.post_er_return = move.ret
        event.post_er_sessions = move.sessions_after
        event.post_er_as_of = move.as_of
        event.post_er_source = move.source
    else:
        # 保留旧值？无则清空 anomaly 等待下次
        if event.post_er_return is None:
            event.outcome_anomaly = None
            event.outcome_expected = expected
            event.outcome_note = judgement.note
            event.outcome_checked_at = now_beijing()
            return judgement
        # 已有历史涨跌则用已存 return 再判一次
        judgement = judge_anomaly(
            expected=expected,
            post_ret=event.post_er_return,
            score_total=event.score_total,
            eliminate_reason=event.eliminate_reason,
        )

    event.outcome_expected = judgement.expected
    event.outcome_anomaly = judgement.anomaly
    event.outcome_note = judgement.note
    event.outcome_checked_at = now_beijing()
    return judgement


async def run_outcome_check(*, lookback_days: int | None = None) -> dict:
    """扫描近 N 日已揭晓财报，回填涨跌并标记异常。"""
    from app.database import EarningsEvent, db_session

    lookback = lookback_days if lookback_days is not None else EARNINGS_OUTCOME_LOOKBACK_DAYS
    today = today_bj()
    since = today - timedelta(days=lookback + 1)
    summary = {"checked": 0, "with_move": 0, "anomalies": 0, "skipped": 0}

    with db_session() as db:
        rows = (
            db.query(EarningsEvent)
            .filter(EarningsEvent.earnings_date >= since)
            .filter(EarningsEvent.earnings_date <= today + timedelta(days=1))
            .all()
        )
        past = [
            e
            for e in rows
            if is_release_past_bj(e.earnings_date, e.session or "TBD")
        ]
        moves: dict[int, PostErMove | None] = {}
        need_live: list = []
        for event in past:
            summary["checked"] += 1
            move = await fetch_post_er_move(
                event.ticker,
                event.earnings_date,
                event.session or "TBD",
                allow_live=False,
            )
            if move is None:
                need_live.append(event)
            moves[event.id] = move

        if need_live:
            live_map = await _batch_resolve_live_prices(
                [e.ticker for e in need_live]
            )
            for event in need_live:
                as_of = pre_earnings_price_as_of(
                    event.earnings_date, event.session or "TBD"
                )
                pre = await _pre_close_only(event.ticker, as_of)
                if not pre:
                    continue
                cand = live_map.get(event.ticker)
                # 常规收盘≈财报前价 → 视为未取到盘后，换源
                if cand is None or abs(cand.price / pre - 1.0) < 0.002:
                    cand = await _resolve_live_post_price(event.ticker, pre)
                if cand is None or abs(cand.price / pre - 1.0) < 0.002:
                    continue
                moves[event.id] = PostErMove(
                    pre_close=pre,
                    last_price=cand.price,
                    ret=(cand.price - pre) / pre,
                    sessions_after=0,
                    as_of=cand.as_of,
                    source=cand.source,
                )

        for event in past:
            move = moves.get(event.id)
            judgement = apply_outcome_to_event(event, move)
            if move is not None or event.post_er_return is not None:
                summary["with_move"] += 1
            if judgement.anomaly:
                summary["anomalies"] += 1
            if judgement.expected == "skip":
                summary["skipped"] += 1
            db.add(event)

    logger.info("earnings outcome check: %s", summary)
    return summary


async def _pre_close_only(ticker: str, as_of: date) -> float | None:
    from app.market_data import fetch_daily_closes

    closes = await fetch_daily_closes(ticker, lookback_days=40)
    pre = [(d, c) for d, c in closes if d <= as_of and c is not None]
    if not pre or not pre[-1][1] or pre[-1][1] <= 0:
        return None
    return float(pre[-1][1])


async def _batch_resolve_live_prices(tickers: list[str]) -> dict[str, _LivePx]:
    """批量解析盘后价：TickDB/热力图 → CNBC → Yahoo AH → Finnhub。"""
    import asyncio

    uniq = list(dict.fromkeys(tickers))
    out: dict[str, _LivePx] = {}
    if not uniq:
        return out

    # 1) TickDB 等（现已解析 post_market_quote）
    try:
        from app.market_data import fetch_quotes

        quotes, src_label = await fetch_quotes(uniq)
        for t in uniq:
            q = quotes.get(t) or {}
            px = q.get("price")
            if px is not None and float(px) > 0:
                out[t] = _LivePx(float(px), today_bj(), f"heatmap:{src_label}")
    except Exception:
        logger.debug("batch heatmap quotes failed", exc_info=True)

    # 2) 仍缺或疑似未含盘后：CNBC ExtendedMktQuote
    need_cnbc = list(uniq)  # 对全部用 CNBC 校验；若变动更大则覆盖
    for t in need_cnbc:
        cnbc = await _fetch_cnbc_extended(t)
        if not cnbc:
            await asyncio.sleep(0.15)
            continue
        prev = out.get(t)
        # 无旧值，或 CNBC 相对旧值变动显著（盘后反应）→ 采用 CNBC
        if prev is None or abs(cnbc.price / prev.price - 1.0) >= 0.002:
            # 若已有盘后且与 CNBC 接近，保留原 source；否则用 CNBC
            if prev is None or abs(cnbc.price / prev.price - 1.0) >= 0.01:
                out[t] = cnbc
        await asyncio.sleep(0.15)

    # 3) 仍缺：Yahoo AH
    still = [t for t in uniq if t not in out]
    for t in still:
        ah = await _fetch_yahoo_extended_last(t)
        if ah:
            out[t] = ah
        await asyncio.sleep(0.4)

    # 4) Finnhub
    still = [t for t in uniq if t not in out]
    for t in still:
        fh = await _fetch_finnhub_quote(t)
        if fh:
            out[t] = fh

    return out


def anomaly_to_dict(event) -> dict:
    """网站异常区单行。"""
    ret = event.post_er_return
    return {
        "id": event.id,
        "ticker": event.ticker,
        "company_name": event.company_name,
        "earnings_date": event.earnings_date.isoformat() if event.earnings_date else None,
        "session": event.session,
        "score_total": event.score_total,
        "eliminate_reason": event.eliminate_reason,
        "push_eligible": bool(event.push_eligible),
        "post_er_return": ret,
        "post_er_return_pct": None if ret is None else round(ret * 100, 2),
        "post_er_sessions": event.post_er_sessions,
        "post_er_as_of": event.post_er_as_of.isoformat() if event.post_er_as_of else None,
        "post_er_source": event.post_er_source,
        "outcome_expected": event.outcome_expected,
        "outcome_expected_label": EXPECTED_LABELS.get(event.outcome_expected or "", event.outcome_expected),
        "outcome_anomaly": event.outcome_anomaly,
        "outcome_anomaly_label": ANOMALY_LABELS.get(event.outcome_anomaly or "", event.outcome_anomaly),
        "outcome_note": event.outcome_note,
        "outcome_checked_at": event.outcome_checked_at.isoformat()
        if event.outcome_checked_at
        else None,
        "one_liner": event.one_liner,
    }
