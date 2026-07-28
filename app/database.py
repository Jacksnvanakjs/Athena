from pathlib import Path

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL, USE_TURSO
from app.utils import now_beijing


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


def _create_engine():
    if USE_TURSO:
        import sqlalchemy_libsql  # noqa: F401 — registers sqlite+libsql dialect

        return create_engine(
            f"sqlite+{TURSO_DATABASE_URL}?secure=true",
            connect_args={"auth_token": TURSO_AUTH_TOKEN},
        )
    return create_engine(DATABASE_URL, connect_args={"check_same_thread": False})


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine)


def init_db():
    if not USE_TURSO:
        from app.config import DATABASE_URL as resolved_url

        db_path = Path(resolved_url.replace("sqlite:///", ""))
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"无法创建数据库目录: {db_path.parent}") from e
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
