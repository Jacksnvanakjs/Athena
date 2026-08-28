"""查询 90 天内 confirmed A 档记录。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.database import NvdaSignalEvent
from app.nvda_signal.config import NVDA_SIGNAL_PRIOR_A_LOOKBACK_DAYS
from app.utils import now_beijing


def find_prior_a(
    db: Session,
    beneficiary_ticker: str,
) -> NvdaSignalEvent | None:
    since = now_beijing() - timedelta(days=NVDA_SIGNAL_PRIOR_A_LOOKBACK_DAYS)
    return (
        db.query(NvdaSignalEvent)
        .filter(
            NvdaSignalEvent.beneficiary_ticker == beneficiary_ticker.upper(),
            NvdaSignalEvent.signal_tier == "A",
            NvdaSignalEvent.status == "confirmed",
            NvdaSignalEvent.published_at >= since,
        )
        .order_by(NvdaSignalEvent.published_at.desc())
        .first()
    )


def prior_a_days_ago(prior: NvdaSignalEvent) -> int:
    delta = now_beijing() - prior.published_at
    return max(0, delta.days)
