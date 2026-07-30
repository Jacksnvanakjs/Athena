from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine, text
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
