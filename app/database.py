from collections.abc import Callable, Generator
from contextlib import contextmanager
import logging
from pathlib import Path
import threading
import time
from typing import TypeVar

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import (
    DATABASE_URL,
    TURSO_AUTH_TOKEN,
    TURSO_CONNECT_RETRIES,
    TURSO_CONNECT_TIMEOUT_SEC,
    TURSO_DATABASE_URL,
    TURSO_DATABASE_URLS,
    TURSO_EMBEDDED_REPLICA,
    TURSO_EMBEDDED_REPLICA_PATH,
    TURSO_SYNC_INTERVAL_SEC,
    USE_TURSO,
)
from app.utils import now_beijing

T = TypeVar("T")
logger = logging.getLogger(__name__)

STREAM_ERROR_MARKERS = (
    "stream not found",
    "stream has expired",
    "stream expired",
    "hrana_closed",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "temporarily unavailable",
    "network is unreachable",
    "name or service not known",
)

_db_ready = threading.Event()
_startup_lock = threading.Lock()
_schema_ready = False
# turso | sqlite
_active_backend = "turso" if USE_TURSO else "sqlite"
_active_turso_url = TURSO_DATABASE_URL or ""
# 本地 SQLite 无网络冷启动问题，默认视为就绪；Turso 需探测成功后才 set
if not USE_TURSO:
    _db_ready.set()
    _schema_ready = True


class Base(DeclarativeBase):
    pass


class Fund(Base):
    __tablename__ = "funds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    url = Column(String(255), nullable=False)


class QuotaRecord(Base):
    __tablename__ = "quota_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    fund_code = Column(String(10), nullable=False, index=True)
    fund_name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False)
    quota = Column(Float, nullable=False)
    scraped_at = Column(DateTime, nullable=False, default=now_beijing, index=True)


class DealEvent(Base):
    __tablename__ = "deal_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    published_at = Column(DateTime, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False)
    headline = Column(String(500), nullable=False)
    summary = Column(Text, nullable=True)
    source = Column(String(50), nullable=False)
    source_url = Column(String(500), nullable=False, index=True)
    headline_hash = Column(String(32), nullable=False, index=True)
    anchor_name = Column(String(100), nullable=False)
    anchor_ticker = Column(String(20), nullable=True)
    anchor_tier = Column(String(10), nullable=False)
    beneficiary_ticker = Column(String(20), nullable=False, index=True)
    beneficiary_name = Column(String(100), nullable=False)
    beneficiary_tier = Column(String(10), nullable=False)
    beneficiary_market_cap_usd = Column(Float, nullable=True)
    tier_pair = Column(String(20), nullable=False)
    materiality_score = Column(Integer, nullable=False)
    matched_keywords = Column(String(500), nullable=True)
    event_type = Column(String(30), nullable=False, default="compute_deal")
    is_update = Column(Boolean, nullable=False, default=False)
    pushed_at = Column(DateTime, nullable=True)
    push_channel = Column(String(30), nullable=True)
    # 受益方首日股价回测（发稿后首个交易日收盘 vs 前收）
    first_day_return = Column(Float, nullable=True)
    first_day_band = Column(String(10), nullable=True)
    first_day_score = Column(Integer, nullable=True)
    first_day_session_date = Column(Date, nullable=True)
    first_day_anomaly = Column(Boolean, nullable=False, default=False)
    first_day_note = Column(String(200), nullable=True)
    first_day_checked_at = Column(DateTime, nullable=True)


class NvdaSignalEvent(Base):
    __tablename__ = "nvda_signal_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    published_at = Column(DateTime, nullable=False, index=True)
    fetched_at = Column(DateTime, nullable=False)
    headline = Column(String(500), nullable=False)
    summary = Column(Text, nullable=True)
    source = Column(String(50), nullable=False)
    source_url = Column(String(500), nullable=False, index=True)
    headline_hash = Column(String(32), nullable=False, index=True)
    beneficiary_ticker = Column(String(20), nullable=False, index=True)
    beneficiary_name = Column(String(100), nullable=False)
    beneficiary_tier = Column(String(10), nullable=False)
    beneficiary_market_cap_usd = Column(Float, nullable=True)
    beneficiary_role = Column(String(20), nullable=False, default="direct")
    signal_tier = Column(String(20), nullable=False, index=True)
    action_type = Column(String(40), nullable=False)
    materiality_score = Column(Integer, nullable=False)
    confidence = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default="confirmed")
    strategy = Column(String(40), nullable=False)
    buy_window = Column(String(200), nullable=False, default="")
    sell_window = Column(String(200), nullable=False, default="")
    sell_plan_json = Column(Text, nullable=True)
    prior_a_event_id = Column(Integer, nullable=True)
    prior_a_days_ago = Column(Integer, nullable=True)
    position_pct = Column(Float, nullable=False, default=1.0)
    buy_ok = Column(Boolean, nullable=False, default=True)
    chase_risk = Column(String(20), nullable=False, default="low")
    pushed_at = Column(DateTime, nullable=True)
    push_channel = Column(String(30), nullable=True)
    first_day_return = Column(Float, nullable=True)
    first_day_band = Column(String(10), nullable=True)
    first_day_score = Column(Integer, nullable=True)
    first_day_session_date = Column(Date, nullable=True)
    first_day_anomaly = Column(Boolean, nullable=False, default=False)
    first_day_note = Column(String(200), nullable=True)
    first_day_checked_at = Column(DateTime, nullable=True)


class NvdaSignalSeenUrl(Base):
    __tablename__ = "nvda_signal_seen_urls"

    source_url = Column(String(500), primary_key=True)
    headline_hash = Column(String(32), nullable=True, index=True)
    seen_at = Column(DateTime, nullable=False)
    relevant = Column(Boolean, nullable=True)


class DealSeenUrl(Base):
    """已分析过的稿件 URL，避免每轮重复送 LLM，也避免漏掉窗口外的旧稿。"""

    __tablename__ = "deal_seen_urls"

    source_url = Column(String(500), primary_key=True)
    headline_hash = Column(String(32), nullable=True, index=True)
    seen_at = Column(DateTime, nullable=False)
    llm_relevant = Column(Boolean, nullable=True)


class EntityAlias(Base):
    __tablename__ = "entity_aliases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False, unique=True, index=True)
    ticker = Column(String(20), nullable=True)
    unlisted_id = Column(String(50), nullable=True)
    updated_at = Column(DateTime, nullable=False)


class MarketCapCache(Base):
    __tablename__ = "market_cap_cache"

    ticker = Column(String(20), primary_key=True)
    market_cap_usd = Column(Float, nullable=False)
    tier = Column(String(10), nullable=False)
    refreshed_at = Column(DateTime, nullable=False)


class HeatmapSnapshot(Base):
    """美股热力图收盘快照（每个 symbol/sector 按美东交易日存一条）。

    trade_date = 美东交易日（非北京时间）。
    自动入库：美东 16:30（北京次日凌晨 04:30 冬令时 / 05:30 夏令时）。
    change_pct / volume = 该交易日收盘相对昨收的涨跌幅与全日成交量。
    """

    __tablename__ = "heatmap_snapshots"
    __table_args__ = (
        UniqueConstraint("trade_date", "kind", "symbol", name="uq_heatmap_day_kind_symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    kind = Column(String(20), nullable=False, index=True)  # sector | company
    symbol = Column(String(20), nullable=False, index=True)
    name = Column(String(100), nullable=False, default="")
    cn_name = Column(String(100), nullable=False, default="")
    sector_key = Column(String(40), nullable=False, default="", index=True)
    sector_name = Column(String(40), nullable=False, default="")
    price = Column(Float, nullable=False, default=0.0)
    change_pct = Column(Float, nullable=False, default=0.0)
    volume = Column(Float, nullable=False, default=0.0)
    dollar_volume = Column(Float, nullable=False, default=0.0)
    flow_score = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, nullable=False, default=now_beijing)


class EarningsEvent(Base):
    """小公司财报日历：每家公司 × 一次财报。"""

    __tablename__ = "earnings_events"
    __table_args__ = (UniqueConstraint("unique_key", name="uq_earnings_unique_key"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    unique_key = Column(String(40), nullable=False, index=True)
    ticker = Column(String(20), nullable=False, index=True)
    company_name = Column(String(100), nullable=False, default="")
    sector = Column(String(20), nullable=False, default="")
    tier = Column(String(10), nullable=False, default="T2")
    market_cap_usd = Column(Float, nullable=True)
    earnings_date = Column(Date, nullable=False, index=True)
    session = Column(String(10), nullable=False, default="TBD")
    confirmed = Column(Boolean, nullable=False, default=False)
    score_total = Column(Integer, nullable=True)
    score_detail_json = Column(Text, nullable=True)
    eliminate_reason = Column(String(200), nullable=True)
    push_eligible = Column(Boolean, nullable=False, default=False)
    one_liner = Column(String(300), nullable=False, default="")
    risk_oneliner = Column(String(300), nullable=False, default="")
    strategy = Column(String(40), nullable=False, default="POST_ER_BUY_WITHIN_2D")
    buy_window = Column(Text, nullable=False, default="")
    sell_window = Column(Text, nullable=False, default="")
    sell_deadline = Column(Text, nullable=False, default="")
    buy_window_json = Column(Text, nullable=True)
    hold_trading_days_max = Column(Integer, nullable=False, default=2)
    status = Column(String(20), nullable=False, default="upcoming", index=True)
    fetched_at = Column(DateTime, nullable=False)
    scored_at = Column(DateTime, nullable=True)
    pushed_at = Column(DateTime, nullable=True)
    push_channel = Column(String(40), nullable=True)
    push_batch_id = Column(Integer, nullable=True)
    source = Column(String(30), nullable=False, default="finnhub")
    # 财报后涨跌 vs 评分对照（自动回填）
    post_er_return = Column(Float, nullable=True)
    post_er_sessions = Column(Integer, nullable=True)
    post_er_as_of = Column(Date, nullable=True)
    post_er_source = Column(String(40), nullable=True)
    outcome_expected = Column(String(20), nullable=True)
    outcome_anomaly = Column(String(40), nullable=True)
    outcome_note = Column(String(300), nullable=True)
    outcome_checked_at = Column(DateTime, nullable=True)


class EarningsPushBatch(Base):
    """同日合并推送批次。"""

    __tablename__ = "earnings_push_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    earnings_date = Column(Date, nullable=False, index=True)
    tickers_csv = Column(String(500), nullable=False, default="")
    title = Column(String(300), nullable=False, default="")
    content_html = Column(Text, nullable=False, default="")
    pushed_at = Column(DateTime, nullable=False)
    push_channel = Column(String(40), nullable=False, default="")
    success = Column(Boolean, nullable=False, default=False)


class AiMainlineDailySnapshot(Base):
    """AI 主线每日快照：每子线一行；theme_key=_meta 存主线结论。"""

    __tablename__ = "ai_mainline_daily_snapshots"
    __table_args__ = (
        UniqueConstraint("trade_date", "theme_key", name="uq_ai_mainline_day_theme"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_date = Column(Date, nullable=False, index=True)
    theme_key = Column(String(40), nullable=False, index=True)
    ret_1d = Column(Float, nullable=True)
    ret_5d = Column(Float, nullable=True)
    ret_20d = Column(Float, nullable=True)
    rel_1d = Column(Float, nullable=True)
    rel_5d = Column(Float, nullable=True)
    rel_20d = Column(Float, nullable=True)
    breadth = Column(Float, nullable=True)
    rank_5d = Column(Integer, nullable=True)
    n_valid = Column(Integer, nullable=False, default=0)
    payload_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=now_beijing)


def is_turso_stream_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in STREAM_ERROR_MARKERS)


def is_db_ready() -> bool:
    return _db_ready.is_set()


def get_db_backend() -> str:
    """当前实际读写后端：turso | sqlite。"""
    return _active_backend


def get_active_turso_url() -> str:
    return _active_turso_url if _active_backend == "turso" else ""


def _turso_https_url(url: str) -> str:
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://") :]
    return url


def _create_engine(*, backend: str | None = None, turso_url: str | None = None):
    backend = backend or _active_backend
    if backend == "turso":
        import sqlalchemy_libsql  # noqa: F401 — registers sqlite+libsql dialect

        url = turso_url or _active_turso_url or TURSO_DATABASE_URL
        # 注意：libsql connect() 不接受 pysqlite 的 timeout=；启动超时靠子进程探测
        # pool_pre_ping 对远程 libsql 几乎等于多一次往返（孟买约 +1s），默认关闭。
        if TURSO_EMBEDDED_REPLICA and TURSO_EMBEDDED_REPLICA_PATH:
            replica = Path(TURSO_EMBEDDED_REPLICA_PATH)
            replica.parent.mkdir(parents=True, exist_ok=True)
            connect_args: dict = {
                "auth_token": TURSO_AUTH_TOKEN,
                "sync_url": _turso_https_url(url),
            }
            if TURSO_SYNC_INTERVAL_SEC > 0:
                connect_args["sync_interval"] = TURSO_SYNC_INTERVAL_SEC
            return create_engine(
                f"sqlite+libsql:///{replica.resolve()}",
                connect_args=connect_args,
                pool_pre_ping=False,
            )
        return create_engine(
            f"sqlite+{url}?secure=true",
            connect_args={"auth_token": TURSO_AUTH_TOKEN},
            pool_pre_ping=False,
            pool_recycle=300,
        )
    return create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


def _sync_embedded_replica() -> None:
    """拉取远程变更到本地副本；读路径之后走本地文件。"""
    if not (TURSO_EMBEDDED_REPLICA and _active_backend == "turso"):
        return
    t0 = time.perf_counter()
    with engine.connect() as conn:
        raw = conn.connection.dbapi_connection
        sync = getattr(raw, "sync", None)
        if callable(sync):
            sync()
    logger.info(
        "Turso embedded replica 已同步（%.1fs，path=%s）",
        time.perf_counter() - t0,
        TURSO_EMBEDDED_REPLICA_PATH,
    )


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def reset_engine(*, wait: bool = True, backend: str | None = None, turso_url: str | None = None) -> None:
    """丢弃失效连接池并按指定后端重建。"""
    global engine, SessionLocal, _active_backend, _active_turso_url
    if backend is not None:
        _active_backend = backend
    if turso_url is not None:
        _active_turso_url = turso_url
    old = engine
    engine = _create_engine(backend=_active_backend, turso_url=_active_turso_url or None)
    SessionLocal.configure(bind=engine)
    if wait:
        try:
            old.dispose()
        except Exception as exc:
            logger.warning("dispose 旧连接池失败: %s", exc)
        return

    def _dispose_old() -> None:
        try:
            old.dispose()
        except Exception:
            pass

    threading.Thread(target=_dispose_old, name="db-engine-dispose", daemon=True).start()


def check_database() -> None:
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))


def _run_db_boot() -> None:
    """探活；首次成功再 create_all/迁移，避免每次重连都扫全表结构。"""
    global _schema_ready
    check_database()
    if not _schema_ready:
        init_db()
        _schema_ready = True


def _subprocess_turso_ping(database_url: str, auth_token: str) -> None:
    """保留给旧测试；实际探测走 _turso_reachable_in_subprocess。"""
    import sqlalchemy_libsql  # noqa: F401
    from sqlalchemy import create_engine, text as sql_text

    eng = create_engine(
        f"sqlite+{database_url}?secure=true",
        connect_args={"auth_token": auth_token},
    )
    try:
        with eng.connect() as conn:
            conn.execute(sql_text("SELECT 1"))
    finally:
        eng.dispose()


def _turso_reachable_in_subprocess(database_url: str, timeout_sec: float) -> bool:
    """用独立解释器跑 app.turso_ping，避免 spawn/GIL 问题。"""
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env["TURSO_PING_URL"] = database_url
    env["TURSO_PING_TOKEN"] = TURSO_AUTH_TOKEN
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "app.turso_ping"],
            env=env,
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True,
            text=True,
            timeout=max(1.0, timeout_sec),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or "").strip()
        if err:
            logger.warning("Turso ping 子进程失败: %s", err[:240])
        return False
    return True


def _try_activate_turso_url(url: str, timeout_sec: float) -> bool:
    """探测并切到指定 Turso URL；成功则标记 ready。"""
    global _schema_ready
    host = url.replace("libsql://", "").split("/")[0]
    logger.info("探测 Turso 节点 %s（timeout=%.0fs）...", host, timeout_sec)
    if not _turso_reachable_in_subprocess(url, timeout_sec):
        logger.warning("Turso 节点不可达: %s", host)
        return False
    try:
        reset_engine(wait=False, backend="turso", turso_url=url)
        _sync_embedded_replica()
    except Exception as dispose_exc:
        logger.warning("切换 Turso 连接池失败 (%s): %s", host, dispose_exc)
        return False
    _schema_ready = True
    _db_ready.set()
    mode = "embedded-replica" if TURSO_EMBEDDED_REPLICA else "remote"
    logger.info("数据库可达（Turso %s，%s）", host, mode)
    return True


def try_startup_db(timeout_sec: float | None = None) -> bool:
    """
    带超时的启动探测：只连 Turso（主 URL + FALLBACKS 副本），不回退 SQLite。

    探测在子进程中进行：libsql 握手会长期占用 GIL，线程超时无法真正打断。
    """
    if not _startup_lock.acquire(blocking=False):
        logger.info("跳过重复的数据库探测（已有探测在进行）")
        return is_db_ready() and get_db_backend() == "turso"

    timeout = float(TURSO_CONNECT_TIMEOUT_SEC if timeout_sec is None else timeout_sec)
    timeout = max(1.0, timeout)
    try:
        if USE_TURSO and TURSO_AUTH_TOKEN and TURSO_DATABASE_URLS:
            # 先试上次成功的节点，再试其余（多区域副本备用）
            urls = list(TURSO_DATABASE_URLS)
            if _active_turso_url and _active_turso_url in urls:
                urls = [_active_turso_url] + [u for u in urls if u != _active_turso_url]
            for round_i in range(TURSO_CONNECT_RETRIES):
                for url in urls:
                    if _try_activate_turso_url(url, timeout):
                        return True
                if round_i + 1 < TURSO_CONNECT_RETRIES:
                    logger.info("Turso 第 %s 轮全失败，立即再试…", round_i + 1)

            logger.warning(
                "所有 Turso 节点探测失败（每节点 %.0fs × %s 轮，候选 %s 个），后台继续重试",
                timeout,
                TURSO_CONNECT_RETRIES,
                len(urls),
            )
            # 周期探测失败时不要清掉已就绪状态，否则网页会间歇性断库
            if not is_db_ready():
                try:
                    reset_engine(wait=False, backend="turso")
                except Exception:
                    pass
            return is_db_ready()

        # 未配置 Turso：仅此时允许纯本地 sqlite
        try:
            reset_engine(wait=False, backend="sqlite")
            _run_db_boot()
        except BaseException as exc:  # noqa: BLE001
            logger.warning("本地 SQLite 启动失败: %s", exc)
            _db_ready.clear()
            return False
        _db_ready.set()
        logger.info("数据库就绪（sqlite，未配置 Turso）")
        return True
    finally:
        _startup_lock.release()


def run_with_db_retry(operation: Callable[[Session], T]) -> T:
    if USE_TURSO and not is_db_ready():
        raise RuntimeError("database not ready; reconnecting turso")
    if not is_db_ready():
        raise RuntimeError("database not ready; reconnecting")
    last_error: BaseException | None = None
    for attempt in range(2):
        db = SessionLocal()
        try:
            return operation(db)
        except Exception as exc:
            last_error = exc
            if attempt == 0 and _active_backend == "turso" and is_turso_stream_error(exc):
                # 流错误：软重置连接池，仍留在 Turso；由后台换节点
                reset_engine(wait=False, backend="turso")
                continue
            raise
        finally:
            db.close()
    assert last_error is not None
    raise last_error


def _ensure_sqlite_columns() -> None:
    """create_all 不会给已有表加列；本地/Turso 共用显式 ALTER。"""
    alters = (
        ("earnings_events", "post_er_return", "FLOAT"),
        ("earnings_events", "post_er_sessions", "INTEGER"),
        ("earnings_events", "post_er_as_of", "DATE"),
        ("earnings_events", "post_er_source", "VARCHAR(40)"),
        ("earnings_events", "outcome_expected", "VARCHAR(20)"),
        ("earnings_events", "outcome_anomaly", "VARCHAR(40)"),
        ("earnings_events", "outcome_note", "VARCHAR(300)"),
        ("earnings_events", "outcome_checked_at", "DATETIME"),
        ("deal_events", "first_day_return", "FLOAT"),
        ("deal_events", "first_day_band", "VARCHAR(10)"),
        ("deal_events", "first_day_score", "INTEGER"),
        ("deal_events", "first_day_session_date", "DATE"),
        ("deal_events", "first_day_anomaly", "BOOLEAN DEFAULT 0"),
        ("deal_events", "first_day_note", "VARCHAR(200)"),
        ("deal_events", "first_day_checked_at", "DATETIME"),
        ("nvda_signal_events", "first_day_return", "FLOAT"),
        ("nvda_signal_events", "first_day_band", "VARCHAR(10)"),
        ("nvda_signal_events", "first_day_score", "INTEGER"),
        ("nvda_signal_events", "first_day_session_date", "DATE"),
        ("nvda_signal_events", "first_day_anomaly", "BOOLEAN DEFAULT 0"),
        ("nvda_signal_events", "first_day_note", "VARCHAR(200)"),
        ("nvda_signal_events", "first_day_checked_at", "DATETIME"),
    )
    with engine.begin() as conn:
        for table, column, coltype in alters:
            try:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            except Exception:
                # 非 sqlite 方言时跳过（新环境靠 create_all）
                continue
            names = {r[1] for r in rows}
            if column in names:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))


def init_db():
    if get_db_backend() != "turso":
        from app.config import DATABASE_URL as resolved_url

        db_path = Path(resolved_url.replace("sqlite:///", ""))
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"无法创建数据库目录: {db_path.parent}") from e
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()


def get_db() -> Generator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
