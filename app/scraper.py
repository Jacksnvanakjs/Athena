import asyncio
import random
import re

import httpx

from app.config import RANDOM_DELAY_MAX, RANDOM_DELAY_MIN
from app.utils import FundInfo, parse_quota_status


async def fetch_fund_status(client: httpx.AsyncClient, fund: FundInfo) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    try:
        resp = await client.get(fund.url, headers=headers, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        match = re.search(
            r'交易状态：</span><span class="staticCell">([^<]+(?:<span>[^<]+</span>)?[^<]*)</span>',
            html,
        )
        if not match:
            return {
                "code": fund.code,
                "name": fund.name,
                "status": "未知",
                "quota": 0.0,
                "raw": "",
                "error": "未找到交易状态",
            }

        raw_text = match.group(1).replace("<span>", "").replace("</span>", "")
        status, quota = parse_quota_status(raw_text)
        return {
            "code": fund.code,
            "name": fund.name,
            "status": status,
            "quota": quota,
            "raw": raw_text,
            "error": None,
        }
    except Exception as e:
        return {
            "code": fund.code,
            "name": fund.name,
            "status": "错误",
            "quota": 0.0,
            "raw": "",
            "error": str(e),
        }


async def scrape_all_funds(funds: list[FundInfo]) -> list[dict]:
    results = []
    async with httpx.AsyncClient() as client:
        for i, fund in enumerate(funds):
            if i > 0:
                delay = random.uniform(RANDOM_DELAY_MIN, RANDOM_DELAY_MAX)
                await asyncio.sleep(delay)
            result = await fetch_fund_status(client, fund)
            results.append(result)
    return results
