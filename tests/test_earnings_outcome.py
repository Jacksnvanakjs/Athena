"""财报后涨跌 vs 评分异常判定单测。"""

from app.earnings_monitor.outcome import (
    ANOMALY_FALSE_NEGATIVE,
    ANOMALY_FALSE_POSITIVE,
    expected_direction,
    judge_anomaly,
)


def test_high_score_expects_bullish():
    assert expected_direction(score_total=90, push_eligible=True, eliminate_reason=None) == "bullish"


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
