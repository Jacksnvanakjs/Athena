import logging
from urllib.parse import quote

import httpx

from app.config import (
    BARK_DEVICE_KEY,
    BARK_GROUP,
    BARK_SERVER_URL,
    PUSHPLUS_TOKEN,
)

logger = logging.getLogger(__name__)


async def send_pushplus(title: str, content: str) -> bool:
    if not PUSHPLUS_TOKEN:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": PUSHPLUS_TOKEN,
                    "title": title,
                    "content": content,
                    "template": "txt",
                },
                timeout=15,
            )
            data = resp.json()
            ok = data.get("code") == 200
            if not ok:
                logger.warning("PushPlus 失败: %s", data)
            return ok
    except Exception as exc:
        logger.warning("PushPlus 异常: %s", exc)
        return False


async def send_bark(title: str, content: str) -> bool:
    """Bark iOS 推送（官方 api.day.app，无需国内实名）。"""
    if not BARK_DEVICE_KEY:
        return False
    body = (content or "").strip() or title
    # Bark 单条 body 过长可能失败，截断保留标题信息
    if len(body) > 3500:
        body = body[:3490].rstrip() + "…"
    payload = {
        "device_key": BARK_DEVICE_KEY,
        "title": (title or "Athena")[:200],
        "body": body,
        "group": BARK_GROUP,
        "level": "timeSensitive",
    }
    url = f"{BARK_SERVER_URL}/push"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=15)
            data = resp.json() if resp.content else {}
            ok = resp.status_code == 200 and int(data.get("code", -1)) == 200
            if not ok:
                # 兼容旧路径：/{key}/{title}/{body}
                path = (
                    f"{BARK_SERVER_URL}/{quote(BARK_DEVICE_KEY, safe='')}/"
                    f"{quote(payload['title'], safe='')}/{quote(body[:500], safe='')}"
                )
                resp2 = await client.get(path, timeout=15)
                data2 = resp2.json() if resp2.content else {}
                ok = resp2.status_code == 200 and int(data2.get("code", -1)) == 200
                if not ok:
                    logger.warning(
                        "Bark 失败: status=%s body=%s fallback=%s",
                        resp.status_code,
                        data,
                        data2,
                    )
            return ok
    except Exception as exc:
        logger.warning("Bark 异常: %s", exc)
        return False


def successful_channels(results: dict) -> list[str]:
    return [name for name, ok in results.items() if ok]


async def notify(title: str, content: str) -> dict:
    results = {}
    if BARK_DEVICE_KEY:
        results["bark"] = await send_bark(title, content)
    if PUSHPLUS_TOKEN:
        results["pushplus"] = await send_pushplus(title, content)
    if not results:
        logger.warning("未配置任何推送通道（BARK_DEVICE_KEY / PUSHPLUS_TOKEN）")
    return results


def format_quota(quota: float) -> str:
    if quota == 0:
        return "0（暂停申购）"
    return f"{quota:.2f} 元"


def build_change_message(changes: list[dict]) -> str:
    lines = ["<b>基金额度变化通知</b><br><br>"]
    for c in changes:
        old_str = format_quota(c["old_quota"])
        new_str = format_quota(c["new_quota"])
        lines.append(f"📊 <b>{c['name']}</b> ({c['code']})<br>")
        lines.append(f"   状态: {c['old_status']} → {c['new_status']}<br>")
        lines.append(f"   额度: {old_str} → {new_str}<br><br>")
    return "".join(lines)


def build_collective_change_message(changes: list[dict], total: int) -> str:
    lines = [
        f"<b>⚠️ 额度集体变化警报</b><br>",
        f"共 {total} 支基金中有 {len(changes)} 支额度发生变化（≥1/3）<br><br>",
        "<table border='1' cellpadding='5' cellspacing='0'>",
        "<tr><th>基金</th><th>代码</th><th>原额度</th><th>新额度</th><th>状态变化</th></tr>",
    ]
    for c in changes:
        lines.append(
            f"<tr><td>{c['name']}</td><td>{c['code']}</td>"
            f"<td>{format_quota(c['old_quota'])}</td>"
            f"<td>{format_quota(c['new_quota'])}</td>"
            f"<td>{c['old_status']} → {c['new_status']}</td></tr>"
        )
    lines.append("</table>")
    return "".join(lines)
