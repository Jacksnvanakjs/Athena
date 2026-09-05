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
# Bark（iOS）：App 打开后复制 key；默认走官方 https://api.day.app，无需自建、无需实名
BARK_DEVICE_KEY = (os.getenv("BARK_DEVICE_KEY") or "").strip()
BARK_SERVER_URL = (os.getenv("BARK_SERVER_URL") or "https://api.day.app").rstrip("/")
BARK_GROUP = (os.getenv("BARK_GROUP") or "Athena").strip() or "Athena"
TURSO_DATABASE_URL = _normalize_turso_url(os.getenv("TURSO_DATABASE_URL", "")) if os.getenv("TURSO_DATABASE_URL") else ""
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")
USE_TURSO = bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)
# 额外 Turso URL（逗号分隔）。仅当官方提供「同库」多节点时再用；
# AWS Edge Replicas 已弃用，勿把另一份独立库填进来（会双写分裂）。
_TURSO_EXTRA = [
    _normalize_turso_url(u.strip())
    for u in os.getenv("TURSO_DATABASE_URL_FALLBACKS", "").split(",")
    if u.strip()
]
TURSO_DATABASE_URLS = list(
    dict.fromkeys(
        ([TURSO_DATABASE_URL] if TURSO_DATABASE_URL else []) + _TURSO_EXTRA
    )
)
# 启动/探测 Turso 时的超时（秒）；超时先起 HTTP，后台只重试 Turso（不切 SQLite）
TURSO_CONNECT_TIMEOUT_SEC = float(os.getenv("TURSO_CONNECT_TIMEOUT_SEC", "45"))
# 后台重连间隔（秒）
TURSO_RECONNECT_INTERVAL_SEC = float(os.getenv("TURSO_RECONNECT_INTERVAL_SEC", "15"))
# 单次启动探测内对每个节点的重试次数
TURSO_CONNECT_RETRIES = max(1, int(os.getenv("TURSO_CONNECT_RETRIES", "2")))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# 本地 Embedded Replica：读走本地文件（亚毫秒），写仍转发 Turso 主库。
# 孟买/东京远程每条查询约 0.5–2s；开副本后页面会快一个数量级。首次 sync 较慢。
TURSO_EMBEDDED_REPLICA = _env_flag("TURSO_EMBEDDED_REPLICA", True)
TURSO_EMBEDDED_REPLICA_PATH = os.getenv(
    "TURSO_EMBEDDED_REPLICA_PATH", str(DATA_DIR / "turso_embedded.db")
)
# 自动增量同步间隔（秒）；0 表示仅启动时手动 sync
TURSO_SYNC_INTERVAL_SEC = max(0, int(float(os.getenv("TURSO_SYNC_INTERVAL_SEC", "30"))))


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
DEAL_POLL_INTERVAL_MIN = int(os.getenv("DEAL_POLL_INTERVAL_MIN", "2"))
DEAL_ADMIN_TOKEN = os.getenv("DEAL_ADMIN_TOKEN", "")
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "AthenaDealMonitor contact@example.com")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")
TICKDB_API_KEY = os.getenv("TICKDB_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# 可选：逗号分隔多把 Gemini Key，429/配额时轮询（单 key 仍用 GEMINI_API_KEY）
_GEMINI_EXTRA = [
    k.strip()
    for k in os.getenv("GEMINI_API_KEYS", "").split(",")
    if k.strip()
]
GEMINI_API_KEYS = list(
    dict.fromkeys(
        ([GEMINI_API_KEY] if GEMINI_API_KEY else []) + _GEMINI_EXTRA
    )
)
DEAL_USE_LLM = os.getenv("DEAL_USE_LLM", "true").lower() == "true"
DEAL_LLM_MODEL = os.getenv("DEAL_LLM_MODEL", "gemini-3.6-flash")
# 发稿超过 N 天：不入库（消 IR/聚合器历史稿造成的假 lag）；0=关闭
DEAL_INGEST_MAX_AGE_DAYS = int(os.getenv("DEAL_INGEST_MAX_AGE_DAYS", "3"))
# 发稿超过 N 天：若已入库则不推送（兜底；正常应由 INGEST 先行丢弃）
DEAL_PUSH_MAX_AGE_DAYS = int(os.getenv("DEAL_PUSH_MAX_AGE_DAYS", "3"))
# 列表默认隐藏软整合/融资/空话（首日回测≥70 仍保留作对照）
DEAL_HIDE_WEAK_QUALITY = os.getenv("DEAL_HIDE_WEAK_QUALITY", "true").lower() == "true"
# 分数与首日回测分差≥此值视为分差大（重打分/筛选）
DEAL_SCORE_OUTCOME_GAP = int(os.getenv("DEAL_SCORE_OUTCOME_GAP", "15"))
DEAL_SCORE_OUTCOME_GAP_DISPLAY = int(os.getenv("DEAL_SCORE_OUTCOME_GAP_DISPLAY", "20"))

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

# ── 小公司财报日历监控 ──
EARNINGS_MONITOR_ENABLED = os.getenv("EARNINGS_MONITOR_ENABLED", "true").lower() == "true"
EARNINGS_PUSH_ENABLED = os.getenv("EARNINGS_PUSH_ENABLED", "true").lower() == "true"
EARNINGS_PUSH_DAYS_BEFORE = int(os.getenv("EARNINGS_PUSH_DAYS_BEFORE", "2"))
EARNINGS_PUSH_ALLOW_T_MINUS_1 = os.getenv("EARNINGS_PUSH_ALLOW_T_MINUS_1", "true").lower() == "true"
EARNINGS_PUSH_ALLOW_T_DAY = os.getenv("EARNINGS_PUSH_ALLOW_T_DAY", "false").lower() == "true"
EARNINGS_PUSH_MIN_SCORE = int(os.getenv("EARNINGS_PUSH_MIN_SCORE", "75"))
EARNINGS_WEB_MIN_SCORE = int(os.getenv("EARNINGS_WEB_MIN_SCORE", "0"))
EARNINGS_LOOKAHEAD_DAYS = int(os.getenv("EARNINGS_LOOKAHEAD_DAYS", "90"))
EARNINGS_SCORE_LOOKAHEAD_DAYS = int(os.getenv("EARNINGS_SCORE_LOOKAHEAD_DAYS", "14"))
EARNINGS_CALENDAR_REFRESH_HOURS = int(os.getenv("EARNINGS_CALENDAR_REFRESH_HOURS", "6"))
EARNINGS_CALENDAR_SOURCE = os.getenv("EARNINGS_CALENDAR_SOURCE", "finnhub")
EARNINGS_STRATEGY = os.getenv("EARNINGS_STRATEGY", "POST_ER_BUY_WITHIN_2D")
EARNINGS_HOLD_TRADING_DAYS_MAX = int(os.getenv("EARNINGS_HOLD_TRADING_DAYS_MAX", "2"))
EARNINGS_CHASE_GAP_PCT_BLOCK = float(os.getenv("EARNINGS_CHASE_GAP_PCT_BLOCK", "15"))
# 已废弃：不再用 $150B 硬淘汰；大市值 AI 硬件（如 DELL）可评分推送
# EARNINGS_MAX_CAP_USD 保留读取以免旧环境报错，评分逻辑已忽略
EARNINGS_MAX_CAP_USD = float(os.getenv("EARNINGS_MAX_CAP_USD", "999999999999"))
# 财报后涨跌 vs 评分：异常监控（供网站「异常区」与后续改机制）
EARNINGS_OUTCOME_LOOKBACK_DAYS = int(os.getenv("EARNINGS_OUTCOME_LOOKBACK_DAYS", "14"))
EARNINGS_OUTCOME_FALSE_POS_PCT = float(os.getenv("EARNINGS_OUTCOME_FALSE_POS_PCT", "5"))
EARNINGS_OUTCOME_FALSE_NEG_PCT = float(os.getenv("EARNINGS_OUTCOME_FALSE_NEG_PCT", "8"))

# ── AI 主线（子板块相对强弱）──
AI_MAINLINE_ENABLED = os.getenv("AI_MAINLINE_ENABLED", "true").lower() == "true"
AI_MAINLINE_CONFIRM_DAYS = int(os.getenv("AI_MAINLINE_CONFIRM_DAYS", "3"))
AI_MAINLINE_MIN_REL_5D = float(os.getenv("AI_MAINLINE_MIN_REL_5D", "1.0"))
AI_MAINLINE_MIN_BREADTH = float(os.getenv("AI_MAINLINE_MIN_BREADTH", "0.55"))
AI_MAINLINE_MIN_VALID = int(os.getenv("AI_MAINLINE_MIN_VALID", "3"))
AI_MAINLINE_PUSH_ENABLED = os.getenv("AI_MAINLINE_PUSH_ENABLED", "false").lower() == "true"
AI_MAINLINE_PUSH_COOLDOWN_DAYS = int(os.getenv("AI_MAINLINE_PUSH_COOLDOWN_DAYS", "5"))

# ── 数据自检 / 缺失补全（部署打断定时任务后自动回填）──
SELF_HEAL_ENABLED = os.getenv("SELF_HEAL_ENABLED", "true").lower() == "true"
SELF_HEAL_INTERVAL_MIN = int(os.getenv("SELF_HEAL_INTERVAL_MIN", "20"))
SELF_HEAL_STARTUP_DELAY_SEC = int(os.getenv("SELF_HEAL_STARTUP_DELAY_SEC", "45"))

RANDOM_DELAY_MIN = 1.0
RANDOM_DELAY_MAX = 3.0
