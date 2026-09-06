"""财报后涨跌 vs 评分异常判定单测。"""

from app.earnings_monitor.outcome import (
    ANOMALY_FALSE_NEGATIVE,
    ANOMALY_FALSE_POSITIVE,
    expected_direction,
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
