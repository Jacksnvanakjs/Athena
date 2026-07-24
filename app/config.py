import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _writable_data_dir() -> Path:
    if env_dir := os.getenv("DATA_DIR"):
        path = Path(env_dir)
    else:
        path = BASE_DIR / "data"
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        return path
    except OSError:
        fallback = Path("/tmp/athena-data")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _writable_data_dir()


def _normalize_turso_url(url: str) -> str:
    url = url.strip()
    if url.startswith("https://"):
        return "libsql://" + url[len("https://") :]
    if url.startswith("http://"):
        return "libsql://" + url[len("http://") :]
    if url.startswith("libsql://"):
        return url
    return f"libsql://{url}"


def _resolve_database_url() -> str:
    explicit = os.getenv("DATABASE_URL")
    if explicit and explicit.startswith("sqlite:///"):
        db_path = Path(explicit.replace("sqlite:///", ""))
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            probe = db_path.parent / ".write_test"
            probe.write_text("ok")
            probe.unlink()
            return explicit
        except OSError:
            pass
    return f"sqlite:///{DATA_DIR}/funds.db"


PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")
TURSO_DATABASE_URL = _normalize_turso_url(os.getenv("TURSO_DATABASE_URL", "")) if os.getenv("TURSO_DATABASE_URL") else ""
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)
DATABASE_URL = _resolve_database_url()
FUNDS_SOURCE_FILE = os.getenv("FUNDS_SOURCE_FILE", str(BASE_DIR / "额度数据来源.txt"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
SCRAPE_SECRET = os.getenv("SCRAPE_SECRET", "")
PORT = int(os.getenv("PORT", "8000"))

SCRAPE_HOURS = [9, 18]
RANDOM_DELAY_MIN = 1.0
RANDOM_DELAY_MAX = 3.0
