"""日线：收盘价 / OHLC 多源：东财 → Yahoo → Stooq → AKShare。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.market_data.cascade import first_success

logger = logging.getLogger(__name__)

_YAHOO_HEADERS = {
    "User-Agent": "Mozilla/5.0 AthenaMarketData/1.0",
    "Referer": "https://finance.yahoo.com/",
}

_EM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
}
_EM_ULIST_URLS = (
    "https://push2.eastmoney.com/api/qt/ulist.np/get",
    "https://82.push2.eastmoney.com/api/qt/ulist.np/get",
)
_EM_KLINE_URLS = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://82.push2his.eastmoney.com/api/qt/stock/kline/get",
    "https://push2delay.eastmoney.com/api/qt/stock/kline/get",
)
_EM_MARKETS = (105, 106, 107)
# symbol → 东财市场码（进程内缓存，避免反复扫 ulist）
_EM_MARKET_CACHE: dict[str, int] = {}
_EM_CACHE_LOADED = False
_EM_CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "em_us_market_cache.json"
_DAILY_CLOSES_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "daily_closes_cache"

# 东财全进程串行限流：并发/轰炸会触发 IP 级断连（空 klines / RemoteDisconnected）
_EM_GATE = asyncio.Lock()
_EM_NEXT_OK = 0.0
_EM_MIN_INTERVAL = 0.55
_EM_COOL_UNTIL = 0.0

_YAHOO_COOL_UNTIL = 0.0
_NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _load_em_market_cache() -> None:
    global _EM_CACHE_LOADED
    if _EM_CACHE_LOADED:
        return
    _EM_CACHE_LOADED = True
    try:
        if _EM_CACHE_PATH.is_file():
            raw = json.loads(_EM_CACHE_PATH.read_text(encoding="utf-8"))
            for k, v in (raw or {}).items():
                sym = str(k).upper().strip()
                try:
                    mkt = int(v)
                except (TypeError, ValueError):
                    continue
                if sym and mkt in _EM_MARKETS:
                    _EM_MARKET_CACHE.setdefault(sym, mkt)
    except Exception as exc:
        logger.debug("load em market cache failed: %s", exc)


def _persist_em_market_cache() -> None:
    try:
        _EM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: _EM_MARKET_CACHE[k] for k in sorted(_EM_MARKET_CACHE)}
        _EM_CACHE_PATH.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug("persist em market cache failed: %s", exc)


def seed_eastmoney_markets(mapping: dict[str, int]) -> None:
    """供报价 ulist 回填市场码，加速后续日 K。"""
    _load_em_market_cache()
    changed = False
    for sym, mkt in mapping.items():
        s = str(sym or "").upper().strip()
        try:
            m = int(mkt)
        except (TypeError, ValueError):
            continue
        if not s or m not in _EM_MARKETS:
            continue
        if _EM_MARKET_CACHE.get(s) != m:
            _EM_MARKET_CACHE[s] = m
            changed = True
    if changed:
        _persist_em_market_cache()


# (date, open, high, low, close)
Bar = tuple[date, float, float, float, float]


def _normalize_closes(rows: list[tuple[date, float]]) -> list[tuple[date, float]]:
    cleaned = [(d, float(c)) for d, c in rows if d and c is not None and float(c) > 0]
    cleaned.sort(key=lambda x: x[0])
    out: dict[date, float] = {}
    for d, c in cleaned:
        out[d] = c
    return sorted(out.items(), key=lambda x: x[0])


def _normalize_bars(rows: list[Bar]) -> list[Bar]:
    cleaned: list[Bar] = []
    for row in rows:
        if not row or row[0] is None:
            continue
        d, o, h, l, c = row
        try:
            o, h, l, c = float(o), float(h), float(l), float(c)
        except (TypeError, ValueError):
            continue
        if c <= 0:
            continue
        if o <= 0:
            o = c
        if h <= 0:
            h = max(o, c)
        if l <= 0:
            l = min(o, c)
        cleaned.append((d, o, h, l, c))
    cleaned.sort(key=lambda x: x[0])
    out: dict[date, Bar] = {}
    for row in cleaned:
        out[row[0]] = row
    return [out[k] for k in sorted(out)]


async def _bars_from_yahoo(ticker: str, lookback_days: int) -> list[Bar]:
    """Yahoo 日 K：优先 curl_cffi Chrome 指纹；遇 429/403 全进程冷却，勿狂打。"""
    global _YAHOO_COOL_UNTIL
    span = max(lookback_days + 40, 60)
    loop = asyncio.get_running_loop()
    if _YAHOO_COOL_UNTIL > loop.time():
        return []

    def _sync_cffi() -> list[Bar]:
        try:
            from curl_cffi import requests as creq
        except ImportError:
            return []
        sess = creq.Session(impersonate="chrome")
        urls = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
        )
        params = {"range": f"{span}d", "interval": "1d"}
        for url in urls:
            try:
                resp = sess.get(url, params=params, timeout=20)
            except Exception:
                continue
            if resp.status_code in (429, 403):
                return []  # 调用方设冷却
            if resp.status_code != 200:
                continue
            text = resp.text or ""
            if not text.lstrip().startswith("{"):
                continue
            try:
                result = (resp.json().get("chart") or {}).get("result") or []
            except Exception:
                continue
            if not result:
                continue
            ts = result[0].get("timestamp") or []
            q = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            opens = q.get("open") or []
            highs = q.get("high") or []
            lows = q.get("low") or []
            closes = q.get("close") or []
            out: list[Bar] = []
            for i, t in enumerate(ts):
                cl = closes[i] if i < len(closes) else None
                if cl is None:
                    continue
                d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
                o = opens[i] if i < len(opens) and opens[i] is not None else cl
                h = highs[i] if i < len(highs) and highs[i] is not None else cl
                l = lows[i] if i < len(lows) and lows[i] is not None else cl
                out.append((d, float(o), float(h), float(l), float(cl)))
            if out:
                return _normalize_bars(out)
        return []

    bars = await asyncio.to_thread(_sync_cffi)
    if bars:
        return bars

    # httpx 兜底（本机常被 TLS/IP 限流）
    urls = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
    )
    params = {"range": f"{span}d", "interval": "1d"}
    async with httpx.AsyncClient(
        headers=_YAHOO_HEADERS, timeout=25, follow_redirects=True
    ) as client:
        for url in urls:
            resp = await client.get(url, params=params)
            if resp.status_code in (429, 403):
                _YAHOO_COOL_UNTIL = loop.time() + 45.0
                logger.warning("Yahoo %s on %s; cool 45s (do not hammer)", resp.status_code, ticker)
                return []
            if resp.status_code != 200:
                continue
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype and not (resp.text or "").lstrip().startswith("{"):
                _YAHOO_COOL_UNTIL = loop.time() + 30.0
                return []
            result = (resp.json().get("chart") or {}).get("result") or []
            if not result:
                continue
            ts = result[0].get("timestamp") or []
            q = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            opens = q.get("open") or []
            highs = q.get("high") or []
            lows = q.get("low") or []
            closes = q.get("close") or []
            out: list[Bar] = []
            for i, t in enumerate(ts):
                cl = closes[i] if i < len(closes) else None
                if cl is None:
                    continue
                d = datetime.fromtimestamp(int(t), tz=timezone.utc).date()
                o = opens[i] if i < len(opens) and opens[i] is not None else cl
                h = highs[i] if i < len(highs) and highs[i] is not None else cl
                l = lows[i] if i < len(lows) and lows[i] is not None else cl
                out.append((d, float(o), float(h), float(l), float(cl)))
            if out:
                return _normalize_bars(out)
    # cffi 也空：多半 IP 限流
    if not bars:
        _YAHOO_COOL_UNTIL = max(_YAHOO_COOL_UNTIL, loop.time() + 20.0)
    return []


def _parse_nasdaq_money(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "")
    if not text or text in {"-", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


async def _bars_from_nasdaq(ticker: str, lookback_days: int) -> list[Bar]:
    """Nasdaq.com 官方历史（无需 Key）；limit 拉满，宁缺勿错。"""
    t = (ticker or "").upper().strip()
    if not t:
        return []
    end = date.today()
    start = end - timedelta(days=max(lookback_days + 40, 90))
    params = {
        "assetclass": "stocks",
        "fromdate": start.isoformat(),
        "todate": end.isoformat(),
        "limit": "120",
    }
    async with httpx.AsyncClient(
        headers=_NASDAQ_HEADERS, timeout=20, follow_redirects=True
    ) as client:
        try:
            resp = await client.get(
                f"https://api.nasdaq.com/api/quote/{t}/historical", params=params
            )
        except Exception as exc:
            logger.debug("nasdaq historical %s: %s", t, exc)
            return []
    if resp.status_code != 200:
        return []
    try:
        data = resp.json().get("data") or {}
    except Exception:
        return []
    rows = ((data.get("tradesTable") or {}).get("rows")) or []
    out: list[Bar] = []
    for row in rows:
        raw_d = (row.get("date") or "").strip()
        c = _parse_nasdaq_money(row.get("close"))
        if not raw_d or c is None or c <= 0:
            continue
        try:
            # 09/14/2026
            d = datetime.strptime(raw_d[:10], "%m/%d/%Y").date()
        except ValueError:
            continue
        o = _parse_nasdaq_money(row.get("open")) or c
        h = _parse_nasdaq_money(row.get("high")) or c
        l = _parse_nasdaq_money(row.get("low")) or c
        out.append((d, float(o), float(h), float(l), float(c)))
    return _normalize_bars(out)


async def _from_nasdaq(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    bars = await _bars_from_nasdaq(ticker, lookback_days)
    return _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])


async def _from_yahoo(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    bars = await _bars_from_yahoo(ticker, lookback_days)
    return _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])


async def _em_get_json(
    client: httpx.AsyncClient, urls: tuple[str, ...], params: dict
) -> dict:
    """东财 JSON。全进程串行限流；断连/空包视为限流，拉长冷却，绝不把空 klines 当成功。"""
    global _EM_NEXT_OK, _EM_COOL_UNTIL
    last_exc: Exception | None = None
    async with _EM_GATE:
        loop = asyncio.get_running_loop()
        now = loop.time()
        wait = max(_EM_NEXT_OK, _EM_COOL_UNTIL) - now
        if wait > 0:
            await asyncio.sleep(wait)
        for idx, url in enumerate(urls):
            attempts = 2 if idx == 0 else 1
            for attempt in range(attempts):
                try:
                    resp = await client.get(url, params=params)
                    _EM_NEXT_OK = loop.time() + _EM_MIN_INTERVAL
                    if resp.status_code != 200:
                        await asyncio.sleep(0.4 * (attempt + 1))
                        continue
                    data = resp.json()
                    if not isinstance(data, dict):
                        return {}
                    # 空 klines + HTTP 200 = 东财封禁信号，勿当有效
                    payload = data.get("data")
                    if isinstance(payload, dict) and "klines" in payload:
                        if not (payload.get("klines") or []):
                            _EM_COOL_UNTIL = loop.time() + 8.0
                            logger.warning(
                                "eastmoney empty klines (likely IP throttle); cool 8s"
                            )
                            return {}
                    return data
                except Exception as exc:
                    last_exc = exc
                    _EM_COOL_UNTIL = loop.time() + min(30.0, 4.0 * (attempt + 1 + idx))
                    await asyncio.sleep(0.5 * (attempt + 1))
        if last_exc:
            logger.debug("eastmoney request failed: %s", last_exc)
        return {}


def load_daily_closes_cache(
    symbols: list[str], *, min_rows: int = 21, require_fresh: bool = True
) -> dict[str, list[tuple[date, float]]]:
    """读本地日 K 缓存。过期（落后于最近已收盘美东交易日）默认不采用，避免钉死旧涨跌。"""
    out: dict[str, list[tuple[date, float]]] = {}
    for sym in symbols:
        path = _DAILY_CLOSES_CACHE_DIR / f"{sym.upper()}.json"
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            rows = raw.get("closes") or []
            parsed: list[tuple[date, float]] = []
            for item in rows:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                d = date.fromisoformat(str(item[0])[:10])
                c = float(item[1])
                if c > 0:
                    parsed.append((d, c))
            parsed = _normalize_closes(parsed)
            if len(parsed) < min_rows:
                continue
            if require_fresh and not closes_are_fresh(parsed):
                continue
            out[sym.upper()] = parsed
        except Exception as exc:
            logger.debug("daily closes cache read %s: %s", sym, exc)
    return out


def save_daily_closes_cache(closes_map: dict[str, list[tuple[date, float]]]) -> None:
    if not closes_map:
        return
    try:
        _DAILY_CLOSES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return
    now = datetime.now(timezone.utc).isoformat()
    for sym, rows in closes_map.items():
        if not rows:
            continue
        path = _DAILY_CLOSES_CACHE_DIR / f"{sym.upper()}.json"
        try:
            payload = {
                "symbol": sym.upper(),
                "updated": now,
                "closes": [[d.isoformat(), float(c)] for d, c in rows],
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            logger.debug("daily closes cache write %s: %s", sym, exc)


def closes_are_fresh(rows: list[tuple[date, float]]) -> bool:
    """最后一根日 K 不早于「最近已收盘美东交易日」前一个交易日（时区缓冲）。"""
    if not rows:
        return False
    last = rows[-1][0]
    try:
        from app.utils import last_completed_us_session

        session = last_completed_us_session()
    except Exception:
        return True
    return last >= session - timedelta(days=1)


def validate_daily_closes(
    rows: list[tuple[date, float]], *, min_rows: int = 21
) -> list[tuple[date, float]]:
    """只保留真实升序正收盘；不够长返回空，禁止把残缺序列当完整日 K 落库。"""
    cleaned = _normalize_closes(rows)
    if len(cleaned) < min_rows:
        return []
    # 日期必须严格递增且无未来日
    today = date.today() + timedelta(days=1)
    prev: date | None = None
    for d, c in cleaned:
        if c <= 0 or d > today:
            return []
        if prev is not None and d <= prev:
            return []
        prev = d
    return cleaned


def persist_daily_closes(
    closes_map: dict[str, list[tuple[date, float]]], *, source: str = ""
) -> None:
    """磁盘 + Turso 同时落盘；只写通过校验且新鲜的序列，禁止堆无效/过期。"""
    clean: dict[str, list[tuple[date, float]]] = {}
    for sym, rows in (closes_map or {}).items():
        ok = validate_daily_closes(rows or [], min_rows=6)
        if not ok:
            continue
        if not closes_are_fresh(ok):
            logger.debug("skip persist %s: not fresh (last=%s)", sym, ok[-1][0])
            continue
        clean[sym.upper()] = ok
    if not clean:
        return
    save_daily_closes_cache(clean)
    upsert_daily_closes_db(clean, source=source)


def load_daily_closes_db(
    symbols: list[str], *, min_rows: int = 6, require_fresh: bool = True
) -> dict[str, list[tuple[date, float]]]:
    uniq = [s.upper().strip() for s in symbols if s and str(s).strip()]
    if not uniq:
        return {}
    try:
        from app.database import DailyCloseBar, SessionLocal
    except Exception:
        return {}
    out: dict[str, list[tuple[date, float]]] = {}
    try:
        with SessionLocal() as db:
            rows = (
                db.query(DailyCloseBar)
                .filter(DailyCloseBar.ticker.in_(uniq))
                .order_by(DailyCloseBar.ticker, DailyCloseBar.trade_date)
                .all()
            )
        by: dict[str, list[tuple[date, float]]] = {}
        for row in rows:
            if not row.close or row.close <= 0:
                continue
            by.setdefault(row.ticker.upper(), []).append(
                (row.trade_date, float(row.close))
            )
        for sym, parsed in by.items():
            parsed = _normalize_closes(parsed)
            if len(parsed) < min_rows:
                continue
            if require_fresh and not closes_are_fresh(parsed):
                continue
            out[sym] = parsed
    except Exception as exc:
        logger.warning("load daily closes from db failed: %s", exc)
    return out


def upsert_daily_closes_db(
    closes_map: dict[str, list[tuple[date, float]]], *, source: str = ""
) -> int:
    if not closes_map:
        return 0
    try:
        from app.database import DailyCloseBar, SessionLocal
        from app.utils import now_beijing
    except Exception:
        return 0
    tickers = [s.upper().strip() for s in closes_map if s]
    if not tickers:
        return 0
    n = 0
    try:
        with SessionLocal() as db:
            existing = (
                db.query(DailyCloseBar)
                .filter(DailyCloseBar.ticker.in_(tickers))
                .all()
            )
            by = {(r.ticker.upper(), r.trade_date): r for r in existing}
            now = now_beijing()
            for ticker, rows in closes_map.items():
                t = ticker.upper().strip()
                for d, c in rows or []:
                    if not d or c is None or float(c) <= 0:
                        continue
                    c = float(c)
                    key = (t, d)
                    row = by.get(key)
                    if row:
                        if abs(row.close - c) > 1e-6 or (source and row.source != source):
                            row.close = c
                            if source:
                                row.source = source
                            row.updated_at = now
                            n += 1
                    else:
                        rec = DailyCloseBar(
                            ticker=t,
                            trade_date=d,
                            close=c,
                            source=source or "",
                            updated_at=now,
                        )
                        db.add(rec)
                        by[key] = rec
                        n += 1
            db.commit()
    except Exception as exc:
        logger.warning("upsert daily closes db failed: %s", exc)
        return 0
    return n


def _parse_em_klines(klines: list[str] | None, lookback_days: int) -> list[Bar]:
    """东财日 K：``date,open,close,high,low,volume,amount,...``。"""
    out: list[Bar] = []
    for line in klines or []:
        parts = str(line).split(",")
        if len(parts) < 5:
            continue
        try:
            d = date.fromisoformat(parts[0][:10])
            o = float(parts[1])
            c = float(parts[2])
            h = float(parts[3])
            l = float(parts[4])
        except (TypeError, ValueError):
            continue
        if c > 0:
            out.append((d, o, h, l, c))
    out = _normalize_bars(out)
    if lookback_days and len(out) > lookback_days + 40:
        out = out[-(lookback_days + 40) :]
    return out


async def _resolve_eastmoney_markets(
    client: httpx.AsyncClient, symbols: list[str]
) -> dict[str, int]:
    """解析美股 secid 市场码：磁盘/内存缓存优先，缺口再 ulist。"""
    _load_em_market_cache()
    uniq = [s.upper().strip() for s in symbols if s and str(s).strip()]
    out: dict[str, int] = {}
    for sym in uniq:
        cached = _EM_MARKET_CACHE.get(sym)
        if cached is not None:
            out[sym] = cached
    missing = [s for s in uniq if s not in out]
    if not missing:
        return out

    for mkt in _EM_MARKETS:
        still = [s for s in missing if s not in out]
        if not still:
            break
        # 逐只解析，避免多 secid 批量触发东财断连
        for sym in still:
            data = await _em_get_json(
                client,
                _EM_ULIST_URLS,
                {"fltt": "2", "secids": f"{mkt}.{sym}", "fields": "f12,f14,f2"},
            )
            for item in ((data.get("data") or {}).get("diff")) or []:
                got = str(item.get("f12") or "").upper()
                px = item.get("f2")
                if got == sym and px not in (None, "", "-"):
                    out[sym] = mkt
                    _EM_MARKET_CACHE[sym] = mkt
                    break
            await asyncio.sleep(0.05)
    if any(s in out for s in missing):
        _persist_em_market_cache()
    return out


async def _bars_from_eastmoney_secid(
    client: httpx.AsyncClient,
    market: int,
    ticker: str,
    lookback_days: int,
    *,
    try_fallback_markets: bool = False,
) -> list[Bar]:
    lmt = max(lookback_days + 20, 40)
    data = await _em_get_json(
        client,
        _EM_KLINE_URLS,
        {
            "secid": f"{market}.{ticker}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "klt": "101",
            "fqt": "1",
            "end": "20500101",
            "lmt": str(lmt),
        },
    )
    klines = ((data.get("data") or {}).get("klines")) or []
    bars = _parse_em_klines(klines, lookback_days)
    if bars or not try_fallback_markets:
        return bars
    # 单票路径才扫其它市场，避免批量时 3× 放大请求
    for mkt in _EM_MARKETS:
        if mkt == market:
            continue
        data = await _em_get_json(
            client,
            _EM_KLINE_URLS,
            {
                "secid": f"{mkt}.{ticker}",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "klt": "101",
                "fqt": "1",
                "end": "20500101",
                "lmt": str(lmt),
            },
        )
        klines = ((data.get("data") or {}).get("klines")) or []
        bars = _parse_em_klines(klines, lookback_days)
        if bars:
            _EM_MARKET_CACHE[ticker] = mkt
            _persist_em_market_cache()
            return bars
    return []


async def _bars_from_eastmoney(ticker: str, lookback_days: int) -> list[Bar]:
    t = (ticker or "").upper().strip()
    if not t:
        return []
    async with httpx.AsyncClient(
        headers=_EM_HEADERS,
        timeout=12,
        follow_redirects=True,
        trust_env=False,  # 东财走直连；经海外代理易断连
    ) as client:
        mapping = await _resolve_eastmoney_markets(client, [t])
        mkt = mapping.get(t)
        if mkt is None:
            return []
        return await _bars_from_eastmoney_secid(
            client, mkt, t, lookback_days, try_fallback_markets=True
        )


async def _from_eastmoney(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    bars = await _bars_from_eastmoney(ticker, lookback_days)
    return _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])


async def fetch_eastmoney_closes_many(
    symbols: list[str],
    *,
    lookback_days: int = 60,
    concurrency: int = 1,
) -> dict[str, list[tuple[date, float]]]:
    """批量东财日线：默认并发 1 + 间隔，避免 IP 封禁。空结果不写入。"""
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not uniq:
        return {}
    _load_em_market_cache()
    out: dict[str, list[tuple[date, float]]] = {}
    async with httpx.AsyncClient(
        headers=_EM_HEADERS,
        timeout=12,
        follow_redirects=True,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        mapping = {s: _EM_MARKET_CACHE[s] for s in uniq if s in _EM_MARKET_CACHE}
        need_resolve = [s for s in uniq if s not in mapping]
        if need_resolve:
            resolved = await _resolve_eastmoney_markets(client, need_resolve)
            mapping.update(resolved)

        # 硬上限 2：东财并发是封禁主因
        sem = asyncio.Semaphore(max(1, min(concurrency, 2)))

        async def one(sym: str) -> None:
            mkt = mapping.get(sym)
            if mkt is None:
                return
            async with sem:
                bars = await _bars_from_eastmoney_secid(
                    client, mkt, sym, lookback_days
                )
                await asyncio.sleep(0.35)
            if len(bars) >= 6:
                out[sym] = _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])

        keys = list(mapping.keys())
        for i in range(0, len(keys), 4):
            await asyncio.gather(*[one(s) for s in keys[i : i + 4]])
            if i + 4 < len(keys):
                await asyncio.sleep(0.6)
    logger.info(
        "eastmoney daily closes %s/%s (mapped %s)",
        len(out),
        len(uniq),
        len(mapping),
    )
    return out


async def _bars_from_stooq(ticker: str, lookback_days: int) -> list[Bar]:
    sym = f"{ticker.lower()}.us"
    url = "https://stooq.com/q/d/l/"
    params = {"s": sym, "i": "d"}
    async with httpx.AsyncClient(
        timeout=8,
        headers={"User-Agent": "Mozilla/5.0 AthenaMarketData/1.0"},
        follow_redirects=True,
    ) as client:
        resp = await client.get(url, params=params)
        if resp.status_code != 200:
            return []
        text = resp.text.strip()
        # 反爬/挑战页常为短 HTML，勿当 CSV 解析
        if (
            not text
            or text.lower().startswith("<!")
            or text.lower().startswith("<html")
            or "text/html" in (resp.headers.get("content-type") or "").lower()
        ):
            return []
        reader = csv.DictReader(io.StringIO(text))
        out: list[Bar] = []
        for row in reader:
            raw_d = (row.get("Date") or row.get("date") or "").strip()
            raw_o = (row.get("Open") or row.get("open") or "").strip()
            raw_h = (row.get("High") or row.get("high") or "").strip()
            raw_l = (row.get("Low") or row.get("low") or "").strip()
            raw_c = (row.get("Close") or row.get("close") or "").strip()
            if not raw_d or not raw_c:
                continue
            try:
                d = date.fromisoformat(raw_d[:10])
                c = float(raw_c)
                o = float(raw_o) if raw_o else c
                h = float(raw_h) if raw_h else c
                l = float(raw_l) if raw_l else c
            except ValueError:
                continue
            if c > 0:
                out.append((d, o, h, l, c))
        out = _normalize_bars(out)
        if lookback_days and len(out) > lookback_days + 40:
            out = out[-(lookback_days + 40) :]
        return out


async def _from_stooq(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    bars = await _bars_from_stooq(ticker, lookback_days)
    return _normalize_closes([(d, c) for d, _o, _h, _l, c in bars])


async def _from_akshare_sina(ticker: str, lookback_days: int) -> list[tuple[date, float]]:
    def _sync() -> list[tuple[date, float]]:
        try:
            import akshare as ak
        except ImportError:
            return []
        try:
            df = ak.stock_us_daily(symbol=ticker.upper(), adjust="")
        except Exception:
            try:
                df = ak.stock_us_daily(symbol=ticker.upper())
            except Exception as exc:
                logger.debug("akshare daily %s: %s", ticker, exc)
                return []
        if df is None or getattr(df, "empty", True):
            return []
        cols = {str(c).lower(): c for c in df.columns}
        dcol = cols.get("date") or cols.get("日期")
        ccol = cols.get("close") or cols.get("收盘")
        if dcol is None or ccol is None:
            return []
        out: list[tuple[date, float]] = []
        for _, row in df.iterrows():
            raw_d = row[dcol]
            raw_c = row[ccol]
            try:
                if hasattr(raw_d, "date"):
                    d = raw_d.date()
                else:
                    d = date.fromisoformat(str(raw_d)[:10])
                c = float(raw_c)
            except Exception:
                continue
            if c > 0:
                out.append((d, c))
        out = _normalize_closes(out)
        if lookback_days and len(out) > lookback_days + 40:
            out = out[-(lookback_days + 40) :]
        return out

    return await asyncio.to_thread(_sync)


def _skip_yahoo_daily() -> bool:
    import os

    return os.environ.get("HEATMAP_SKIP_YAHOO", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _daily_close_sources(
    ticker: str,
    lookback_days: int,
    *,
    skip_yahoo: bool,
    skip_akshare: bool = False,
):
    """美股日线多源（first_success 轮动起点，避免总钉死同一源）。

    顺序候选：Nasdaq（免 Key）→ 东财 → 付费源（无 Key 自动跳过）→ Yahoo → Stooq → AKShare。
    空结果 / 限流不算命中，继续下一个；禁止编造。
    """
    from app.market_data.alt_sources import (
        fetch_alpha_vantage_daily_closes,
        fetch_marketstack_daily_closes,
        fetch_polygon_daily_closes,
        fetch_tiingo_daily_closes,
        fetch_twelve_daily_closes,
    )

    t = ticker
    sources: list = [
        ("nasdaq", lambda: _from_nasdaq(t, lookback_days)),
        ("eastmoney", lambda: _from_eastmoney(t, lookback_days)),
    ]
    # 付费源：无 Key 不进轮动，避免空转拖预算
    from app.config import (
        ALPHA_VANTAGE_API_KEY,
        MARKETSTACK_API_KEY,
        POLYGON_API_KEY,
        TIINGO_API_KEY,
        TWELVE_DATA_API_KEY,
    )

    if (ALPHA_VANTAGE_API_KEY or "").strip():
        sources.append(
            (
                "alpha_vantage",
                lambda: fetch_alpha_vantage_daily_closes(t, lookback_days=lookback_days),
            )
        )
    if (TWELVE_DATA_API_KEY or "").strip():
        sources.append(
            ("twelve_data", lambda: fetch_twelve_daily_closes(t, lookback_days=lookback_days))
        )
    if (TIINGO_API_KEY or "").strip():
        sources.append(
            ("tiingo", lambda: fetch_tiingo_daily_closes(t, lookback_days=lookback_days))
        )
    if (POLYGON_API_KEY or "").strip():
        sources.append(
            ("polygon", lambda: fetch_polygon_daily_closes(t, lookback_days=lookback_days))
        )
    if (MARKETSTACK_API_KEY or "").strip():
        sources.append(
            (
                "marketstack",
                lambda: fetch_marketstack_daily_closes(t, lookback_days=lookback_days),
            )
        )
    if not skip_yahoo:
        sources.append(("yahoo", lambda: _from_yahoo(t, lookback_days)))
    sources.append(("stooq", lambda: _from_stooq(t, lookback_days)))
    if not skip_akshare:
        sources.append(("akshare_sina", lambda: _from_akshare_sina(t, lookback_days)))
    return sources


async def fetch_daily_closes(
    ticker: str,
    *,
    lookback_days: int = 120,
    skip_yahoo: bool | None = None,
    skip_akshare: bool = False,
    rotate: bool = True,
) -> list[tuple[date, float]]:
    """日线收盘 [(date, close), ...] 升序；多源自动切换。"""
    t = (ticker or "").upper().strip()
    if not t:
        return []
    if skip_yahoo is None:
        skip_yahoo = _skip_yahoo_daily()

    result = await first_success(
        "daily_closes",
        _daily_close_sources(
            t, lookback_days, skip_yahoo=skip_yahoo, skip_akshare=skip_akshare
        ),
        is_ok=lambda rows: isinstance(rows, list) and len(rows) >= 3,
        context=t,
        rotate=rotate,
    )
    if not result:
        return []
    return list(result.value)


async def fetch_daily_closes_many(
    symbols: list[str],
    *,
    lookback_days: int = 60,
    concurrency: int = 4,
    skip_yahoo: bool | None = None,
    skip_akshare: bool = True,
) -> dict[str, list[tuple[date, float]]]:
    """多标的日线：优先 Nasdaq 串行补齐，再多源轮动；不先狂打东财。

    缺数据的标的不写入（宁缺勿错）。默认跳过 AKShare。
    """
    uniq = list(dict.fromkeys(s.upper().strip() for s in symbols if s and str(s).strip()))
    if not uniq:
        return {}
    if skip_yahoo is None:
        skip_yahoo = _skip_yahoo_daily()

    out: dict[str, list[tuple[date, float]]] = {}

    # 1) Nasdaq 免 Key，低并发，避免触发东财/Yahoo 限流
    sem_nq = asyncio.Semaphore(2)

    async def one_nasdaq(sym: str) -> None:
        async with sem_nq:
            try:
                rows = await _from_nasdaq(sym, lookback_days)
            except Exception as exc:
                logger.debug("nasdaq batch %s: %s", sym, exc)
                return
            ok = validate_daily_closes(rows, min_rows=6)
            if ok:
                out[sym] = ok
            await asyncio.sleep(0.25)

    await asyncio.gather(*[one_nasdaq(s) for s in uniq])
    missing = [s for s in uniq if s not in out]
    if not missing:
        return out

    # 2) 东财慢速补缺口（并发≤2）
    try:
        em = await fetch_eastmoney_closes_many(
            missing, lookback_days=lookback_days, concurrency=1
        )
        for sym, rows in em.items():
            ok = validate_daily_closes(rows, min_rows=6)
            if ok:
                out[sym] = ok
    except Exception as exc:
        logger.warning("eastmoney batch daily closes failed: %s", exc)

    missing = [s for s in uniq if s not in out]
    if not missing:
        return out

    # 3) 其余：多源轮动（含付费源/Yahoo/Stooq），低并发
    gap_conc = 2
    sem = asyncio.Semaphore(gap_conc)

    async def one(sym: str) -> None:
        async with sem:
            try:
                rows = await fetch_daily_closes(
                    sym,
                    lookback_days=lookback_days,
                    skip_yahoo=skip_yahoo,
                    skip_akshare=skip_akshare,
                    rotate=True,
                )
            except Exception as exc:
                logger.debug("daily closes %s failed: %s", sym, exc)
                return
            ok = validate_daily_closes(rows or [], min_rows=6)
            if ok:
                out[sym] = ok
            await asyncio.sleep(0.35)

    await asyncio.gather(*[one(s) for s in missing])
    return out


async def fetch_daily_bars(
    ticker: str,
    *,
    lookback_days: int = 120,
    skip_yahoo: bool | None = None,
) -> list[Bar]:
    """日线 OHLC [(date, open, high, low, close), ...] 升序。"""
    t = (ticker or "").upper().strip()
    if not t:
        return []
    if skip_yahoo is None:
        skip_yahoo = _skip_yahoo_daily()
    sources = [("eastmoney", lambda: _bars_from_eastmoney(t, lookback_days))]
    if not skip_yahoo:
        sources.append(("yahoo", lambda: _bars_from_yahoo(t, lookback_days)))
    sources.append(("stooq", lambda: _bars_from_stooq(t, lookback_days)))

    result = await first_success(
        "daily_bars",
        sources,
        is_ok=lambda rows: isinstance(rows, list) and len(rows) >= 3,
        context=t,
    )
    if not result:
        closes = await fetch_daily_closes(t, lookback_days=lookback_days)
        return [(d, c, c, c, c) for d, c in closes]
    return list(result.value)
