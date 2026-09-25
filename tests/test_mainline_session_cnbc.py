"""主线扩展时段报价：CNBC 会话价解析。"""

from app.heatmap import _parse_cnbc_formatted_quote, _quote_looks_like_rth_close


def test_cnbc_post_uses_extended_vs_prior_close():
    q = {
        "symbol": "NVDA",
        "name": "NVIDIA",
        "last": "224.58",
        "change_pct": "-0.41%",
        "previous_day_closing": "224.58",
        "curmktstatus": "POST_MKT",
        "ExtendedMktQuote": {
            "type": "POST_MKT",
            "last": "223.84",
            "last_timedate": "09/24/26 EDT",
            "change_pct": "-0.33%",
            "volume": "7,500,064",
        },
    }
    row = _parse_cnbc_formatted_quote(q, "NVDA")
    assert row is not None
    assert abs(row["price"] - 223.84) < 1e-6
    # prior ≈ 224.58 / (1 - 0.0041)
    prior = 224.58 / (1 - 0.0041)
    expect = (223.84 - prior) / prior * 100
    assert abs(row["change_pct"] - expect) < 0.05
    assert row.get("quote_time_et") and "20:00" in row["quote_time_et"]
    assert not _quote_looks_like_rth_close(row)


def test_rth_stamp_detector():
    assert _quote_looks_like_rth_close(
        {"quote_time": "2026-09-25 04:00:00", "quote_time_et": "2026-09-24 16:00:00 EDT"}
    )
    assert not _quote_looks_like_rth_close(
        {"quote_time": "2026-09-25 08:00:00", "quote_time_et": "2026-09-24 20:00:00 EDT"}
    )
