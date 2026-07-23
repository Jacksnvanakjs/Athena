#!/usr/bin/env python3
import os

import uvicorn

from app.config import PORT

if __name__ == "__main__":
    reload = os.getenv("ENV", "production") == "development"
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=reload)
