import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
SERVERCHAN_SENDKEY = os.getenv("SERVERCHAN_SENDKEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/data/funds.db")
FUNDS_SOURCE_FILE = os.getenv("FUNDS_SOURCE_FILE", str(BASE_DIR / "额度数据来源.txt"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
SCRAPE_SECRET = os.getenv("SCRAPE_SECRET", "")
PORT = int(os.getenv("PORT", "8000"))

SCRAPE_HOURS = [9, 18]
RANDOM_DELAY_MIN = 1.0
RANDOM_DELAY_MAX = 3.0
