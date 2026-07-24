from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import DATABASE_URL


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
    scraped_at = Column(DateTime, nullable=False, default=datetime.now, index=True)


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)


def init_db():
    db_path = Path(DATABASE_URL.replace("sqlite:///", ""))
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
