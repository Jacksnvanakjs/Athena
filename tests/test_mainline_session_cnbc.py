"""主线扩展时段报价：CNBC 会话价解析。"""

from app.heatmap import _parse_cnbc_formatted_quote, _quote_looks_like_rth_close


def test_cnbc_post_prev_uses_extended_as_public_fallback():
    """CNBC 无 ATS 夜盘；POST_MKT_PREV 作盘后回退（非常规 16:00）。"""
    q = {
        "symbol": "NVDA",
        "name": "NVIDIA",
        "last": "224.58",
        "change_pct": "-0.41%",
        "previous_day_closing": "224.58",
        "curmktstatus": "REG_MKT",
        "ExtendedMktQuote": {
            "type": "POST_MKT_PREV",
            "last": "223.84",
            "last_timedate": "7:59 PM EDT",
            "last_time": "2026-09-24",
            "change_pct": "-0.33%",
            "volume": "7,500,064",
        },
    }
    row = _parse_cnbc_formatted_quote(q, "NVDA")
    assert row is not None
    assert abs(row["price"] - 223.84) < 1e-6
    prior = 224.58 / (1 - 0.0041)
    expect = (223.84 - prior) / prior * 100
    assert abs(row["change_pct"] - expect) < 0.05
    assert row.get("quote_time_et") and "2026-09-24 19:59" in row["quote_time_et"]
    assert not _quote_looks_like_rth_close(row)


def test_cnbc_live_post_uses_extended():
    q = {
        "symbol": "AMD",
        "name": "AMD",
        "last": "629.26",
        "change_pct": "+2.38%",
        "curmktstatus": "POST_MKT",
        "ExtendedMktQuote": {
            "type": "POST_MKT",
            "last": "629.47",
            "last_timedate": "09/24/26 EDT",
            "change_pct": "+0.03%",
            "volume": "824,551",
        },
    }
    row = _parse_cnbc_formatted_quote(q, "AMD")
    assert row is not None
    assert abs(row["price"] - 629.47) < 1e-6
    assert "2026-09-24 20:00" in (row.get("quote_time_et") or "")


def test_rth_stamp_detector():
    assert _quote_looks_like_rth_close(
        {"quote_time": "2026-09-25 04:00:00", "quote_time_et": "2026-09-24 16:00:00 EDT"}
    )
    assert not _quote_looks_like_rth_close(
        {"quote_time": "2026-09-25 07:59:00", "quote_time_et": "2026-09-24 19:59:00 EDT"}
    )
