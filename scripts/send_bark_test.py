#!/usr/bin/env python3
"""向本机配置的 Bark 发一条测试推送。

用法：
  1. App Store 安装 Bark，打开后复制 key
  2. 写入 .env：BARK_DEVICE_KEY=你的key
  3. .venv/bin/python scripts/send_bark_test.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import BARK_DEVICE_KEY, BARK_SERVER_URL  # noqa: E402
from app.notifier import notify  # noqa: E402


async def main() -> int:
    if not BARK_DEVICE_KEY:
        print("未配置 BARK_DEVICE_KEY。请先在 .env / Belmo 填写 Bark App 里的 key。")
        print("官方推送地址默认：", BARK_SERVER_URL)
        return 1
    results = await notify(
        "[Athena] Bark 测试",
        "若手机收到本条，说明 Bark 推送已接通。\n无需 PushPlus 实名。",
    )
    print("结果:", results)
    ok = any(results.values())
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
