"""财报后涨跌 vs 评分异常判定单测。"""

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

from app.earnings_monitor.outcome import (
    ANOMALY_FALSE_NEGATIVE,
    ANOMALY_FALSE_POSITIVE,
    expected_direction,
    fetch_post_er_move,
    judge_anomaly,
)


def test_high_score_expects_bullish():
    assert expected_direction(score_total=90, push_eligible=True, eliminate_reason=None) == "bullish"


def test_elevated_score_expects_bullish():
    """近推送带（如旧 ZS 72）在日历上已偏强，应按看涨做对照。"""
    assert expected_direction(score_total=72, push_eligible=False, eliminate_reason=None) == "bullish"


def test_low_score_expects_bearish():
    assert expected_direction(score_total=61, push_eligible=False, eliminate_reason=None) == "bearish"


def test_e7_expects_bearish():
    assert (
        expected_direction(
            score_total=None,
            push_eligible=False,
            eliminate_reason="E7:财报前10日暴跌",
        )
        == "bearish"
    )


def test_e1_skipped():
    assert (
        expected_direction(
            score_total=None,
            push_eligible=False,
            eliminate_reason="E1:市值T0巨头排除",
        )
        == "skip"
    )


def test_false_positive_high_score_drop():
    j = judge_anomaly(expected="bullish", post_ret=-0.09, score_total=90)
    assert j.anomaly == ANOMALY_FALSE_POSITIVE


def test_false_positive_zs_style_small_drop():
    """高分票跌超 2% 也应标异常（ZS 91→-2.1%）。"""
    j = judge_anomaly(expected="bullish", post_ret=-0.021, score_total=91)
    assert j.anomaly == ANOMALY_FALSE_POSITIVE


def test_aligned_mild_drop_mid_score_no_anomaly():
    """近推送带小跌（未超 2%）→ 不进异常。"""
    j = judge_anomaly(expected="bullish", post_ret=-0.015, score_total=70)
    assert j.anomaly is None


def test_elevated_score_drop_is_anomaly():
    """旧 ZS：72 分却跌 4.5% → 应标高分却大跌（即便未达推送线）。"""
    j = judge_anomaly(expected="bullish", post_ret=-0.045, score_total=72)
    assert j.anomaly == ANOMALY_FALSE_POSITIVE


def test_push_eligible_small_drop_is_anomaly():
    j = judge_anomaly(expected="bullish", post_ret=-0.021, score_total=82)
    assert j.anomaly == ANOMALY_FALSE_POSITIVE


def test_false_negative_low_score_rally():
    j = judge_anomaly(
        expected="bearish",
        post_ret=0.10,
        score_total=None,
        eliminate_reason="E5:财报前30日涨幅27%>25%",
    )
    assert j.anomaly == ANOMALY_FALSE_NEGATIVE


def test_aligned_no_anomaly():
    assert judge_anomaly(expected="bullish", post_ret=0.10, score_total=90).anomaly is None
    assert judge_anomaly(expected="bearish", post_ret=-0.13, score_total=61).anomaly is None


def test_fetch_post_er_move_three_legs():
    """AMC 财报后：开盘 / D1～D5；主字段用 D2。"""
    bars = [
        (date(2026, 9, 1), 98.0, 99.0, 97.0, 98.5),
        (date(2026, 9, 2), 99.0, 101.0, 98.0, 100.0),
        (date(2026, 9, 3), 112.8, 113.0, 103.0, 104.3),
        (date(2026, 9, 4), 104.0, 105.0, 100.0, 101.4),
        (date(2026, 9, 5), 101.0, 106.0, 100.0, 105.0),
        (date(2026, 9, 8), 105.0, 108.0, 104.0, 107.0),
        (date(2026, 9, 9), 107.0, 110.0, 106.0, 109.0),
    ]

    async def _run():
        with patch(
            "app.market_data.fetch_daily_bars",
            new=AsyncMock(return_value=bars),
        ):
            return await fetch_post_er_move("NTSK", date(2026, 9, 2), "AMC")

    move = asyncio.run(_run())
    assert move is not None
    assert abs(move.open_ret - 0.128) < 1e-9
    assert abs(move.d1_ret - 0.043) < 1e-9
    assert abs(move.d2_ret - 0.014) < 1e-9
    assert abs(move.d3_ret - 0.05) < 1e-9
    assert abs(move.d4_ret - 0.07) < 1e-9
    assert abs(move.d5_ret - 0.09) < 1e-9
    assert abs(move.ret - 0.014) < 1e-9
    assert move.sessions_after == 2
