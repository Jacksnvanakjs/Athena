import logging

import httpx

from app.config import PUSHPLUS_TOKEN, SERVERCHAN_SENDKEY

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


async def send_serverchan(title: str, content: str) -> bool:
    if not SERVERCHAN_SENDKEY:
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://sctapi.ftqq.com/{SERVERCHAN_SENDKEY}.send",
                data={"title": title, "desp": content},
                timeout=15,
            )
            data = resp.json()
            ok = data.get("code") == 0
            if not ok:
                logger.warning("Server酱 失败: %s", data)
            return ok
    except Exception as exc:
        logger.warning("Server酱 异常: %s", exc)
        return False


async def notify(title: str, content: str) -> dict:
    results = {}
    if PUSHPLUS_TOKEN:
        results["pushplus"] = await send_pushplus(title, content)
    if SERVERCHAN_SENDKEY:
        results["serverchan"] = await send_serverchan(title, content)
    if not results:
        logger.warning("未配置任何推送通道（PUSHPLUS_TOKEN / SERVERCHAN_SENDKEY）")
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
