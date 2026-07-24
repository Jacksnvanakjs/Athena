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

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR}/funds.db")
FUNDS_SOURCE_FILE = os.getenv("FUNDS_SOURCE_FILE", str(BASE_DIR / "额度数据来源.txt"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
SCRAPE_SECRET = os.getenv("SCRAPE_SECRET", "")
PORT = int(os.getenv("PORT", "8000"))

SCRAPE_HOURS = [9, 18]
RANDOM_DELAY_MIN = 1.0
RANDOM_DELAY_MAX = 3.0
