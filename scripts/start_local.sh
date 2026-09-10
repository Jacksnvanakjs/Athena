#!/bin/zsh
# 本地站：先用 turso CLI 拉云端全库快照，再用纯 SQLite 起服务（不卡页面）。
set -euo pipefail
cd /Users/admin/USA/Athena

if curl -sS -m 1 -o /dev/null http://127.0.0.1:18809/ 2>/dev/null; then
  export HTTPS_PROXY=http://127.0.0.1:18809
  export HTTP_PROXY=http://127.0.0.1:18809
  export ALL_PROXY=http://127.0.0.1:18809
  echo "[athena-local] proxy on"
else
  echo "[athena-local] proxy off"
fi

echo "[athena-local] 从云端同步…"
if ! .venv/bin/python scripts/sync_turso_snapshot.py; then
  echo "[athena-local] 云端同步失败；尝试沿用已有 data/athena_local.db" >&2
  [[ -f data/athena_local.db ]] || exit 1
fi

export TURSO_DATABASE_URL=
export TURSO_AUTH_TOKEN=
export DATABASE_URL="sqlite:////Users/admin/USA/Athena/data/athena_local.db"
export ENABLE_SCHEDULER=false
export RELOAD=false

echo "[athena-local] 启动 http://127.0.0.1:8000 （云端快照）"
exec .venv/bin/python run.py
