#!/usr/bin/env python3
"""用 turso CLI 从云端 dump 到 data/athena_local.db（独立进程，不卡网页）。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DUMP = Path("/tmp/athena-cloud.dump.sql")
DST = ROOT / "data" / "athena_local.db"
DB_NAME = os.getenv("ATHENA_TURSO_DB_NAME", "athena-apac")
TURSO_BIN = os.getenv("TURSO_BIN", str(Path.home() / ".turso" / "turso"))


def main() -> int:
    if not Path(TURSO_BIN).exists():
        print(f"[sync] 找不到 turso CLI: {TURSO_BIN}", file=sys.stderr)
        return 1

    print(f"[sync] dumping cloud db `{DB_NAME}` via turso CLI…")
    env = os.environ.copy()
    with open(DUMP, "w", encoding="utf-8") as out, open("/tmp/athena-dump.err", "w") as err:
        proc = subprocess.run(
            [TURSO_BIN, "db", "shell", DB_NAME, ".dump"],
            stdout=out,
            stderr=err,
            env=env,
            cwd=str(ROOT),
        )
    if proc.returncode != 0:
        print(Path("/tmp/athena-dump.err").read_text()[:500], file=sys.stderr)
        return 2
    if DUMP.stat().st_size < 1000:
        print("[sync] dump 文件过小，失败", file=sys.stderr)
        return 3

    DST.parent.mkdir(parents=True, exist_ok=True)
    for p in (DST, Path(str(DST) + "-wal"), Path(str(DST) + "-shm")):
        if p.exists():
            p.unlink()

    # 写入临时库再替换，避免半成品
    tmp = DST.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    subprocess.check_call(["sqlite3", str(tmp), f".read {DUMP}"])
    tmp.replace(DST)
    print(f"[sync] wrote {DST} ({DST.stat().st_size / 1e6:.1f} MB)")

    # 迁移缺列（必须在清空 Turso env 后 import app）
    os.environ["TURSO_DATABASE_URL"] = ""
    os.environ["TURSO_AUTH_TOKEN"] = ""
    os.environ["DATABASE_URL"] = f"sqlite:///{DST.resolve()}"
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from app.database import init_db, try_startup_db

    assert try_startup_db(5)
    init_db()
    print("[sync] schema migrated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
