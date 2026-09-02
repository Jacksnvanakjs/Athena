"""Yahoo 行情解析：盘前/盘中价量分离。"""

from app.heatmap import _parse_yahoo_meta, _quote_row


def _meta(**kwargs):
    base = {
        "symbol": "TSLA",
        "shortName": "Tesla",
        "previousClose": 348.75,
        "chartPreviousClose": 348.75,
    }
    base.update(kwargs)
    return base


def test_premarket_uses_pre_market_fields_not_regular_volume():
    meta = _meta(
        marketState="PRE",
        preMarketPrice=362.5,
        preMarketChangePercent=-1.43,
        preMarketVolume=310000,
        regularMarketPrice=367.95,
        regularMarketChangePercent=5.51,
        regularMarketVolume=58000000,
    )
    q = _parse_yahoo_meta(meta, None, "TSLA")
    assert q is not None
    assert q["price"] == 362.5
    assert q["change_pct"] == -1.43
    assert q["volume"] == 310000
    assert q["flow_score"] < 0


def test_regular_session_uses_regular_fields():
    meta = _meta(
        marketState="REGULAR",
        regularMarketPrice=340.0,
        regularMarketChangePercent=-2.51,
        regularMarketVolume=45000000,
        preMarketPrice=362.5,
        preMarketChangePercent=-1.43,
    )
    q = _parse_yahoo_meta(meta, None, "TSLA")
    assert q is not None
    assert q["price"] == 340.0
    assert q["change_pct"] == -2.51
    assert q["volume"] == 45000000


def test_flow_score_formula():
    q = _quote_row("GS", name="Goldman", price=1000.0, change_pct=-1.0, volume=100000)
    assert q["dollar_volume"] == 100_000_000
    assert q["flow_score"] == -0.1
