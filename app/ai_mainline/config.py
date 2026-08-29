"""ai_mainline 配置。"""

from pathlib import Path

from app.config import (
    AI_MAINLINE_CONFIRM_DAYS,
    AI_MAINLINE_ENABLED,
    AI_MAINLINE_MIN_BREADTH,
    AI_MAINLINE_MIN_REL_5D,
    AI_MAINLINE_MIN_VALID,
    AI_MAINLINE_PUSH_COOLDOWN_DAYS,
    AI_MAINLINE_PUSH_ENABLED,
    BASE_DIR,
    DATA_DIR,
)

BASKET_CANDIDATES = (
    Path(__file__).resolve().parent / "ai_theme_baskets.json",
    DATA_DIR / "ai_theme_baskets.json",
    BASE_DIR / "data" / "ai_theme_baskets.json",
)

META_KEY = "_meta"
