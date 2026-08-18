from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import DATABASE_URL, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL, USE_TURSO
from app.utils import now_beijing

T = TypeVar("T")

STREAM_ERROR_MARKERS = (
    "stream not found",
    "stream has expired",
    "stream expired",
    "hrana_closed",
)


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
    """美股热力图每日收盘快照（按美东交易日存一条）。"""

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


def is_turso_stream_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in STREAM_ERROR_MARKERS)


def _create_engine():
    if USE_TURSO:
        import sqlalchemy_libsql  # noqa: F401 — registers sqlite+libsql dialect

        return create_engine(
            f"sqlite+{TURSO_DATABASE_URL}?secure=true",
            connect_args={"auth_token": TURSO_AUTH_TOKEN},
            pool_pre_ping=True,
            pool_recycle=300,
        )
    return create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def reset_engine() -> None:
    """丢弃失效的 Turso 连接池，下次请求会建立新连接。"""
    global engine, SessionLocal
    engine.dispose()
    engine = _create_engine()
    SessionLocal.configure(bind=engine)


def check_database() -> None:
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))


def run_with_db_retry(operation: Callable[[Session], T]) -> T:
    last_error: BaseException | None = None
    for attempt in range(2):
        db = SessionLocal()
        try:
            return operation(db)
        except Exception as exc:
            last_error = exc
            if attempt == 0 and USE_TURSO and is_turso_stream_error(exc):
                reset_engine()
                continue
            raise
        finally:
            db.close()
    assert last_error is not None
    raise last_error


def init_db():
    if not USE_TURSO:
        from app.config import DATABASE_URL as resolved_url

        db_path = Path(resolved_url.replace("sqlite:///", ""))
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"无法创建数据库目录: {db_path.parent}") from e
    Base.metadata.create_all(bind=engine)


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
