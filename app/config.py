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
def _resolve_funds_source_file() -> str:
    if env_path := os.getenv("FUNDS_SOURCE_FILE"):
        return env_path
    candidates = (
        BASE_DIR / "文档" / "额度数据来源.txt",
        BASE_DIR / "额度数据来源.txt",
    )
    for path in candidates:
        if path.is_file():
            return str(path)
    return str(candidates[0])


DATABASE_URL = _resolve_database_url()
FUNDS_SOURCE_FILE = _resolve_funds_source_file()
TIMEZONE = os.getenv("TIMEZONE", "Asia/Shanghai")
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() == "true"
SCRAPE_SECRET = os.getenv("SCRAPE_SECRET", "")
PORT = int(os.getenv("PORT", "8000"))

SCRAPE_TIMES = [(11, 5), (14, 35)]
# 卡片「额度变化」高亮保留天数（近 2 个月）
CHANGE_HIGHLIGHT_DAYS = int(os.getenv("CHANGE_HIGHLIGHT_DAYS", "60"))

# ── AI 合作快讯监控 (deal_monitor) ──
DEAL_T0_MIN_CAP = float(os.getenv("DEAL_T0_MIN_CAP", "500000000000"))
DEAL_T1_MIN_CAP = float(os.getenv("DEAL_T1_MIN_CAP", "5000000000"))
DEAL_T2_MAX_CAP = float(os.getenv("DEAL_T2_MAX_CAP", "5000000000"))
DEAL_SCORE_MIN_DEFAULT = int(os.getenv("DEAL_SCORE_MIN_DEFAULT", "55"))
DEAL_SCORE_MIN_T0_T0 = int(os.getenv("DEAL_SCORE_MIN_T0_T0", "70"))
DEAL_SCORE_MIN_T0_T1 = int(os.getenv("DEAL_SCORE_MIN_T0_T1", "60"))
DEAL_SCORE_MIN_T1_T1 = int(os.getenv("DEAL_SCORE_MIN_T1_T1", "65"))
DEAL_DEDUP_DAYS = int(os.getenv("DEAL_DEDUP_DAYS", "7"))
DEAL_MAX_PUSH_PER_HOUR = int(os.getenv("DEAL_MAX_PUSH_PER_HOUR", "10"))
DEAL_MAX_PUSH_PER_BENEFICIARY_24H = int(os.getenv("DEAL_MAX_PUSH_PER_BENEFICIARY_24H", "1"))
DEAL_T0_T0_PUSH_ENABLED = os.getenv("DEAL_T0_T0_PUSH_ENABLED", "true").lower() == "true"
DEAL_T2_T2_PUSH_BOTH = os.getenv("DEAL_T2_T2_PUSH_BOTH", "false").lower() == "true"
DEAL_PUSH_ENABLED = os.getenv("DEAL_PUSH_ENABLED", "true").lower() == "true"
DEAL_POLL_INTERVAL_MIN = int(os.getenv("DEAL_POLL_INTERVAL_MIN", "3"))
DEAL_ADMIN_TOKEN = os.getenv("DEAL_ADMIN_TOKEN", "")
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "AthenaDealMonitor contact@example.com")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEAL_USE_LLM = os.getenv("DEAL_USE_LLM", "true").lower() == "true"
DEAL_LLM_MODEL = os.getenv("DEAL_LLM_MODEL", "gemini-3.6-flash")
# 发稿超过 N 天：仍入库观察，但不推送（防 IR RSS 历史稿误推）
DEAL_PUSH_MAX_AGE_DAYS = int(os.getenv("DEAL_PUSH_MAX_AGE_DAYS", "3"))

# ── 黄仁勋 / NVDA A 档产业动作监控 ──
NVDA_SIGNAL_ENABLED = os.getenv("NVDA_SIGNAL_ENABLED", "true").lower() == "true"
NVDA_SIGNAL_PUSH_ENABLED = os.getenv("NVDA_SIGNAL_PUSH_ENABLED", "true").lower() == "true"
NVDA_SIGNAL_A_PLUS_B_ENABLED = os.getenv("NVDA_SIGNAL_A_PLUS_B_ENABLED", "true").lower() == "true"
NVDA_SIGNAL_USE_LLM = os.getenv("NVDA_SIGNAL_USE_LLM", "false").lower() == "true"
NVDA_SIGNAL_PRIOR_A_LOOKBACK_DAYS = int(os.getenv("NVDA_SIGNAL_PRIOR_A_LOOKBACK_DAYS", "90"))
NVDA_SIGNAL_A_DEDUP_DAYS = int(os.getenv("NVDA_SIGNAL_A_DEDUP_DAYS", "7"))
NVDA_SIGNAL_A_PLUS_B_DEDUP_DAYS = int(os.getenv("NVDA_SIGNAL_A_PLUS_B_DEDUP_DAYS", "14"))
NVDA_SIGNAL_MIN_MATERIALITY_A = int(os.getenv("NVDA_SIGNAL_MIN_MATERIALITY_A", "65"))
NVDA_SIGNAL_MIN_MATERIALITY_A_PLUS_B = int(os.getenv("NVDA_SIGNAL_MIN_MATERIALITY_A_PLUS_B", "55"))
NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A = int(os.getenv("NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A", "80"))
NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A_PLUS_B = int(os.getenv("NVDA_SIGNAL_PUSH_MIN_CONFIDENCE_A_PLUS_B", "72"))
NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A = float(os.getenv("NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A", "0.15"))
NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A_PLUS_B = float(os.getenv("NVDA_SIGNAL_CHASE_GAP_THRESHOLD_A_PLUS_B", "0.10"))
NVDA_SIGNAL_A_PLUS_B_POSITION_PCT = float(os.getenv("NVDA_SIGNAL_A_PLUS_B_POSITION_PCT", "0.50"))
NVDA_SIGNAL_A_PLUS_B_INTRADAY_MAX_GAIN = float(os.getenv("NVDA_SIGNAL_A_PLUS_B_INTRADAY_MAX_GAIN", "0.10"))
NVDA_SIGNAL_A_PLUS_B_STOP_LOSS = float(os.getenv("NVDA_SIGNAL_A_PLUS_B_STOP_LOSS", "-0.05"))

RANDOM_DELAY_MIN = 1.0
RANDOM_DELAY_MAX = 3.0
