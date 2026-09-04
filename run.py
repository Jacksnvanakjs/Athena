#!/usr/bin/env python3
import os

import uvicorn

from app.config import PORT

if __name__ == "__main__":
    # RELOAD=false 可强制关掉热重载（开发时 Turso 卡死容易留下僵死 worker）
    reload_env = os.getenv("RELOAD", "").lower()
    if reload_env in ("0", "false", "no"):
        reload = False
    elif reload_env in ("1", "true", "yes"):
        reload = True
    else:
        reload = os.getenv("ENV", "production") == "development"
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=reload)
