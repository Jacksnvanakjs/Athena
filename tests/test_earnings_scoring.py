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
