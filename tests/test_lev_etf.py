"""科技杠杆 ETF 月度聚合单测。"""

from datetime import date

from app.lev_etf.aggregate import (
    aggregate_monthly,
    build_stats,
    daily_basket_notional,
    filter_year,
)
from app.lev_etf.basket import all_tickers, load_basket


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
    assert abs(by["2026-06"]["notional_bn"] - daily[date(2026, 6, 2)] / 1e9 - daily[date(2026, 6, 3)] / 1e9) < 1e-6

    only_2026 = filter_year(points, "2026")
    assert all(p["month"].startswith("2026") for p in only_2026)
    stats = build_stats(points)
    assert stats["latest"]["month"] == "2026-09"
    assert stats["max"]["month"] in {"2026-06", "2026-09"}
