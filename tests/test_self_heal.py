"""数据自检补全：对库内真实缺口做只读扫描。"""

from app.self_heal import audit_data_gaps


def test_audit_structure():
    result = audit_data_gaps()
    assert "counts" in result
    assert "gaps" in result
    assert "needs_heal" in result
    for key in (
        "earnings_missing_post_er",
        "earnings_unscored_near",
        "deal_missing_first_day",
        "nvda_missing_first_day",
    ):
        assert key in result["counts"]
        assert isinstance(result["gaps"][key], list)


def test_audit_sees_recent_missing_earnings_if_any():
    """若库里仍有已揭晓但无涨跌的财报，应出现在缺口列表。"""
    result = audit_data_gaps()
    missing = result["gaps"]["earnings_missing_post_er"]
    # 不强制一定有缺口（可能已被补全）；有则字段完整
    for g in missing:
        assert g.get("ticker")
        assert g.get("earnings_date")
        assert "session" in g
