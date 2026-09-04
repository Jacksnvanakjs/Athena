"""独立进程探测 Turso：避免 multiprocessing.spawn 在 uvicorn 下误失败。"""

from __future__ import annotations

import os
import sys


def main() -> int:
    url = (os.environ.get("TURSO_PING_URL") or "").strip()
    token = (os.environ.get("TURSO_PING_TOKEN") or "").strip()
    if not url or not token:
        print("missing TURSO_PING_URL/TOKEN", file=sys.stderr)
        return 2
    if url.startswith("https://"):
        url = "libsql://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "libsql://" + url[len("http://") :]
    elif not url.startswith("libsql://"):
        url = f"libsql://{url}"

    import sqlalchemy_libsql  # noqa: F401
    from sqlalchemy import create_engine, text

    eng = create_engine(
        f"sqlite+{url}?secure=true",
        connect_args={"auth_token": token},
    )
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    finally:
        eng.dispose()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"turso ping failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
