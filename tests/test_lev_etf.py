"""科技杠杆 ETF 日度/月度聚合单测。"""

from datetime import date

from app.lev_etf.aggregate import (
    aggregate_monthly,
    build_stats,
    daily_basket_notional,
    filter_year,
    to_daily_points,
)
from app.lev_etf.basket import all_tickers, load_basket
from app.lev_etf.fetch_ohlcv import _parse_stooq_csv


def test_parse_stooq_csv_with_volume():
    text = (
        "Date,Open,High,Low,Close,Volume\n"
        "2026-06-02,49,51,48,50,10000000\n"
        "2026-06-03,50,52,49,51,12000000\n"
        "bad,x,x,x,x,x\n"
    )
    bars = _parse_stooq_csv(text)
    assert bars == [
        (date(2026, 6, 2), 50.0, 10_000_000.0),
        (date(2026, 6, 3), 51.0, 12_000_000.0),
    ]


def test_basket_has_tech_tickers():
    b = load_basket()
    tickers = all_tickers(b)
    assert b["key"] == "tech_lev_etf"
    assert "TQQQ" in tickers
    assert "SOXL" in tickers
    assert "NVDL" in tickers
    assert len(tickers) >= 20


def test_daily_and_monthly_aggregate():
    bars = {
        "TQQQ": [
            (date(2026, 6, 2), 50.0, 10_000_000),
            (date(2026, 6, 3), 51.0, 12_000_000),
            (date(2026, 9, 2), 40.0, 8_000_000),
        ],
        "SOXL": [
            (date(2026, 6, 2), 20.0, 5_000_000),
            (date(2026, 9, 2), 18.0, 4_000_000),
        ],
    }
    daily = daily_basket_notional(bars)
    assert daily[date(2026, 6, 2)] == 50.0 * 10_000_000 + 20.0 * 5_000_000

    day_pts = to_daily_points(daily, start_date=date(2023, 1, 1))
    assert len(day_pts) == 3
    assert day_pts[0]["date"] == "2026-06-02"
    assert abs(day_pts[0]["notional_bn"] - daily[date(2026, 6, 2)] / 1e9) < 1e-6
    assert filter_year(day_pts, "2025") == []
    assert len(filter_year(day_pts, "2026")) == 3

    points = aggregate_monthly(
        daily,
        start_month="2023-01",
        as_of=date(2026, 9, 2),
        current_et_month="2026-09",
    )
    by = {p["month"]: p for p in points}
    assert by["2026-06"]["is_partial"] is False
    assert by["2026-09"]["is_partial"] is True
    assert by["2026-06"]["trading_days"] == 2
    assert abs(
        by["2026-06"]["notional_bn"]
        - daily[date(2026, 6, 2)] / 1e9
        - daily[date(2026, 6, 3)] / 1e9
    ) < 1e-6

    only_2026 = filter_year(points, "2026")
    assert all(p["month"].startswith("2026") for p in only_2026)
    stats = build_stats(points)
    assert stats["latest"]["month"] == "2026-09"
    assert stats["latest"]["label"] == "2026-09"
    assert stats["max"]["month"] in {"2026-06", "2026-09"}
    day_stats = build_stats(day_pts)
    assert day_stats["latest"]["date"] == "2026-09-02"


def test_daily_partial_flag():
    daily = {
        date(2026, 9, 25): 1e9,
        date(2026, 9, 28): 0.5e9,
    }
    pts = to_daily_points(daily, partial_date=date(2026, 9, 28))
    assert pts[0]["is_partial"] is False
    assert pts[1]["is_partial"] is True
    assert pts[1]["date"] == "2026-09-28"


def test_soxl_daily_and_monthly_returns():
    from app.lev_etf.aggregate import (
        attach_soxl_daily_returns,
        attach_soxl_monthly_returns,
        soxl_closes_from_bars,
    )

    bars = {
        "SOXL": [
            (date(2026, 5, 29), 20.0, 1_000_000),
            (date(2026, 6, 2), 22.0, 1_000_000),
            (date(2026, 6, 3), 21.0, 1_000_000),
            (date(2026, 7, 1), 21.0, 1_000_000),
        ]
    }
    closes = soxl_closes_from_bars(bars)
    daily = attach_soxl_daily_returns(
        [
            {"date": "2026-05-29", "notional_bn": 1},
            {"date": "2026-06-02", "notional_bn": 1},
            {"date": "2026-06-03", "notional_bn": 1},
            {"date": "2026-07-01", "notional_bn": 1},
        ],
        closes,
    )
    by = {p["date"]: p for p in daily}
    assert by["2026-05-29"]["soxl_ret_pct"] is None
    assert by["2026-06-02"]["soxl_ret_pct"] == 10.0
    assert by["2026-06-03"]["soxl_ret_pct"] == -4.55
    monthly = attach_soxl_monthly_returns(
        [
            {"month": "2026-05", "notional_bn": 1},
            {"month": "2026-06", "notional_bn": 1},
            {"month": "2026-07", "notional_bn": 1},
        ],
        closes,
    )
    mb = {p["month"]: p for p in monthly}
    assert mb["2026-05"]["soxl_ret_pct"] is None
    assert mb["2026-06"]["soxl_ret_pct"] == 5.0  # 21 vs 20
    assert mb["2026-07"]["soxl_ret_pct"] == 0.0


def test_merge_soxl_fields_keeps_notional():
    from app.lev_etf.pipeline import _merge_soxl_fields

    base = [{"date": "2026-10-01", "notional_bn": 1.2, "soxl_ret_pct": None}]
    extra = [{"date": "2026-10-01", "soxl_ret_pct": 3.5, "soxl_close": 40.0}]
    out = _merge_soxl_fields(base, extra, "date")
    assert out[0]["notional_bn"] == 1.2
    assert out[0]["soxl_ret_pct"] == 3.5
    assert out[0]["soxl_close"] == 40.0

