"""批量按新规则重打合作事件材料性分（可选按首日回测校准）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.deal_monitor.materiality import (
    SCORE_OUTCOME_GAP,
    calibrate_score_toward_outcome,
    classify_deal_quality,
    finalize_materiality_score,
    is_large_score_outcome_gap,
    score_outcome_gap,
)
from app.source_url_guard import is_test_source_url

logger = logging.getLogger(__name__)


def _parse_keywords(raw: str | None) -> list[str]:
    if not raw:
        return []
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [str(x) for x in data]
        except Exception:
            pass
    return [p.strip() for p in text.split(",") if p.strip()]


def compute_deal_rescore(
    event,
    *,
    calibrate: bool = True,
) -> dict[str, Any]:
    text = f"{event.headline or ''}\n{event.summary or ''}"
    matched = _parse_keywords(getattr(event, "matched_keywords", None))
    quality = classify_deal_quality(text)
    rule = finalize_materiality_score(
        text,
        event.source or "",
        matched,
        event_type=getattr(event, "event_type", None),
    )
    final = calibrate_score_toward_outcome(rule, event.first_day_score) if calibrate else rule
    old = int(event.materiality_score or 0)
    return {
        "id": event.id,
        "ticker": getattr(event, "beneficiary_ticker", None),
        "headline": (event.headline or "")[:80],
        "quality": quality,
        "old_score": old,
        "rule_score": rule,
        "new_score": final,
        "first_day_score": event.first_day_score,
        "old_gap": score_outcome_gap(old, event.first_day_score),
        "new_gap": score_outcome_gap(final, event.first_day_score),
        "changed": final != old,
    }


def rescore_deal_events(
    db: Session,
    *,
    lookback_days: int | None = 365,
    gap_only: bool = True,
    gap_threshold: int = SCORE_OUTCOME_GAP,
    calibrate: bool = True,
    dry_run: bool = False,
    limit: int = 500,
    include_hidden: bool = True,
    require_first_day: bool | None = None,
) -> dict[str, Any]:
    """重打 DealEvent 材料性分。

    gap_only=True：处理「当前分差大」或「按新规则仍分差大/分数会变」的有回测条目。
    include_hidden=True：连内容过滤会隐藏的也重打，保证库内一致。
    """
    from datetime import timedelta

    from app.database import DealEvent
    from app.utils import now_beijing

    if require_first_day is None:
        require_first_day = gap_only

    q = db.query(DealEvent)
    if lookback_days and lookback_days > 0:
        since = now_beijing() - timedelta(days=lookback_days)
        q = q.filter(DealEvent.published_at >= since)

    rows = q.order_by(DealEvent.published_at.desc()).limit(max(limit * 5, 500)).all()
    updated: list[dict] = []
    skipped = 0
    considered = 0
    targets: list[tuple[Any, dict]] = []

    for event in rows:
        if is_test_source_url(event.source_url):
            skipped += 1
            continue
        if require_first_day and event.first_day_score is None:
            skipped += 1
            continue

        info = compute_deal_rescore(event, calibrate=calibrate)
        if gap_only:
            old_large = is_large_score_outcome_gap(
                event.materiality_score, event.first_day_score, threshold=gap_threshold
            )
            rule_large = is_large_score_outcome_gap(
                info["rule_score"], event.first_day_score, threshold=gap_threshold
            )
            new_large = is_large_score_outcome_gap(
                info["new_score"], event.first_day_score, threshold=gap_threshold
            )
            # 当前分差大、或规则/校准后仍会动刀的，都纳入
            if not (old_large or rule_large or (info["changed"] and (old_large or rule_large or new_large))):
                # 仍纳入：旧分与新规则分不同，且有回测（完善机制）
                if not (info["changed"] and event.first_day_score is not None):
                    skipped += 1
                    continue
                if abs(info["old_score"] - info["rule_score"]) < 3 and not old_large:
                    skipped += 1
                    continue

        targets.append((event, info))
        if len(targets) >= limit:
            break

    for event, info in targets:
        considered += 1
        if info["changed"]:
            if not dry_run:
                event.materiality_score = info["new_score"]
            updated.append(info)
        else:
            # 未变也记入 samples 便于核对「已对齐」
            if len(updated) < 5:
                updated.append({**info, "unchanged": True})

    if not dry_run and any(u.get("changed") for u in updated):
        try:
            db.commit()
            summary_ok = sum(1 for u in updated if u.get("changed"))
        except Exception:
            db.rollback()
            ok = 0
            for info in updated:
                if not info.get("changed"):
                    continue
                ev = db.query(DealEvent).filter(DealEvent.id == info["id"]).first()
                if not ev:
                    continue
                ev.materiality_score = info["new_score"]
                try:
                    db.commit()
                    ok += 1
                except Exception as exc:
                    db.rollback()
                    logger.warning("rescore commit failed id=%s: %s", info["id"], exc)
            summary_ok = ok
    else:
        summary_ok = 0

    changed = [u for u in updated if u.get("changed")]
    large_before = sum(
        1 for u in changed if u["old_gap"] is not None and abs(u["old_gap"]) >= gap_threshold
    )
    large_after = sum(
        1
        for u in changed
        if u["new_gap"] is not None and abs(u["new_gap"]) >= gap_threshold
    )
    summary = {
        "lookback_days": lookback_days,
        "gap_only": gap_only,
        "gap_threshold": gap_threshold,
        "calibrate": calibrate,
        "dry_run": dry_run,
        "include_hidden": include_hidden,
        "considered": considered,
        "updated": len(changed),
        "committed": summary_ok if not dry_run else 0,
        "skipped": skipped,
        "large_gap_before": large_before,
        "large_gap_after": large_after,
        "samples": changed[:30],
    }
    logger.info(
        "deal rescore: considered=%s updated=%s large %s→%s dry_run=%s",
        considered,
        len(changed),
        large_before,
        large_after,
        dry_run,
    )
    return summary
