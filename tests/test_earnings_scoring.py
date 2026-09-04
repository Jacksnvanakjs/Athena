"""财报评分形态规则单测（无网络）。"""

from app.earnings_monitor.scoring import hard_eliminate, score_candidate, _setup_into_er_score


def test_e5_no_longer_kills_constructive_momentum():
    """GTLB 类：30日大涨不应硬淘汰。"""
    assert (
        hard_eliminate(
            tier="T1",
            market_cap_usd=8e9,
            pre_30d_gain=0.267,
            pre_10d_gain=0.087,
        )
        is None
    )


def test_e7_still_kills_crash_into_er():
    reason = hard_eliminate(
        tier="T1",
        market_cap_usd=39e9,
        pre_30d_gain=-0.05,
        pre_10d_gain=-0.16,
    )
    assert reason and reason.startswith("E7:")


def test_setup_gtlb_constructive_momentum():
    score, label = _setup_into_er_score(
        0.087,
        0.267,
        pre_5d_gain=0.082,
        down_streak=1,
        from_21d_high=-0.031,
    )
    assert label == "constructive_momentum"
    assert score >= 18


def test_setup_dell_healthy_pullback():
    score, label = _setup_into_er_score(
        -0.093,
        -0.009,
        pre_5d_gain=-0.059,
        down_streak=3,
        from_21d_high=-0.141,
    )
    assert label == "healthy_pullback"
    assert score >= 20


def test_setup_snow_still_healthy_pullback():
    """SNOW：月线略负 + 脱离高点，双确认健康回调须维持高分（财报后大涨样本）。"""
    score, label = _setup_into_er_score(
        -0.059,
        -0.035,
        pre_5d_gain=-0.04,
        down_streak=2,
        from_21d_high=-0.093,
    )
    assert label == "healthy_pullback"
    assert score >= 20
    snow = score_candidate(
        sector="AI_SAAS",
        tier="T1",
        session="AMC",
        confirmed=True,
        days_to=1,
        eliminate_reason=None,
        market_cap_usd=60e9,
        pre_10d_gain=-0.059,
        pre_30d_gain=-0.035,
        down_streak=2,
        from_21d_high=-0.093,
    )
    assert snow.score_total is not None and snow.score_total >= 88
    assert snow.push_eligible is True


def test_setup_cien_weak_slide_not_healthy():
    """CIEN：周月同跌不应再标健康回调。"""
    score, label = _setup_into_er_score(
        -0.113,
        -0.139,
        down_streak=2,
    )
    assert label == "weak_slide"
    assert score <= -6


def test_setup_zs_soft_dip_not_healthy():
    """ZS：月线仍正、周线小回调、形态特征缺失 → soft_dip，不是健康回调。"""
    score, label = _setup_into_er_score(
        -0.064,
        0.058,
        down_streak=None,
        from_21d_high=None,
    )
    assert label == "soft_dip"
    assert score <= 6


def test_setup_ai_drift_into_er():
    """AI：10日仅微涨 → drift，不应给可推送级形态分。"""
    score, label = _setup_into_er_score(
        0.0135,
        0.0468,
        down_streak=None,
        from_21d_high=None,
    )
    assert label == "drift"
    assert score <= 6


def test_cien_zs_ai_rescore_below_push():
    """结构分封顶 + 形态修正后，CIEN/ZS/AI 不应再轻松过推送线。"""
    cien = score_candidate(
        sector="AI_NET",
        tier="T1",
        session="BMO",
        confirmed=True,
        days_to=1,
        eliminate_reason=None,
        market_cap_usd=50e9,
        pre_10d_gain=-0.113,
        pre_30d_gain=-0.139,
        down_streak=2,
    )
    zs = score_candidate(
        sector="AI_SEC",
        tier="T1",
        session="AMC",
        confirmed=True,
        days_to=1,
        eliminate_reason=None,
        market_cap_usd=28e9,
        pre_10d_gain=-0.064,
        pre_30d_gain=0.058,
        down_streak=None,
        from_21d_high=None,
    )
    ai = score_candidate(
        sector="AI_SAAS",
        tier="T2",
        session="AMC",
        confirmed=True,
        days_to=1,
        eliminate_reason=None,
        market_cap_usd=1.6e9,
        pre_10d_gain=0.0135,
        pre_30d_gain=0.0468,
    )
    assert cien.score_total is not None and cien.score_total < 75
    assert zs.score_total is not None and zs.score_total < 75
    assert ai.score_total is not None and ai.score_total < 75
    assert cien.push_eligible is False
    assert zs.push_eligible is False
    assert ai.push_eligible is False


def test_setup_mdb_stale_extension():
    score, label = _setup_into_er_score(
        -0.001,
        0.213,
        pre_5d_gain=0.072,
        down_streak=1,
        from_21d_high=-0.081,
    )
    assert label == "stale_extension"
    assert score <= -8


def test_setup_panw_late_bounce():
    score, label = _setup_into_er_score(
        -0.032,
        0.043,
        pre_5d_gain=0.065,
        down_streak=1,
        from_21d_high=-0.086,
    )
    assert label == "late_bounce"
    assert score <= -6


def test_setup_hpe_weak_off_highs():
    score, label = _setup_into_er_score(
        -0.024,
        -0.011,
        pre_5d_gain=-0.062,
        down_streak=0,
        from_21d_high=-0.134,
    )
    assert label == "weak_off_highs"
    assert score <= -6


def test_setup_ntap_bleeding():
    score, label = _setup_into_er_score(
        -0.070,
        -0.051,
        pre_5d_gain=-0.067,
        down_streak=5,
        from_21d_high=-0.127,
    )
    assert label == "bleeding"
    assert score <= -10


def test_score_ranking_winners_above_losers():
    """同一基础条件下，赢家形态应明显高于输家，且过推送线。"""
    common = dict(
        sector="AI_SAAS",
        tier="T1",
        session="AMC",
        confirmed=True,
        days_to=1,
        eliminate_reason=None,
        market_cap_usd=20e9,
    )
    gtlb = score_candidate(
        **common,
        pre_30d_gain=0.267,
        pre_10d_gain=0.087,
        pre_5d_gain=0.082,
        down_streak=1,
        from_21d_high=-0.031,
    )
    mdb = score_candidate(
        **common,
        pre_30d_gain=0.213,
        pre_10d_gain=-0.001,
        pre_5d_gain=0.072,
        down_streak=1,
        from_21d_high=-0.081,
    )
    panw = score_candidate(
        sector="AI_SEC",
        tier="T1",
        session="AMC",
        confirmed=True,
        days_to=1,
        eliminate_reason=None,
        market_cap_usd=295e9,
        pre_30d_gain=0.043,
        pre_10d_gain=-0.032,
        pre_5d_gain=0.065,
        down_streak=1,
        from_21d_high=-0.086,
    )
    assert gtlb.score_total is not None and gtlb.score_total >= 85
    assert gtlb.push_eligible is True
    assert mdb.score_total is not None and mdb.score_total < 70
    assert panw.score_total is not None and panw.score_total < 70
    assert gtlb.score_total > mdb.score_total
    assert gtlb.score_total > panw.score_total
