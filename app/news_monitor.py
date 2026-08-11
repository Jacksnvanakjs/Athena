"""
AI产业链合作新闻监控（无预算版）。

目标：
1) 快速拉取候选新闻（RSS）
2) 用关键词规则筛选“合作/协议 + AI + 算力/云/基础设施”类消息
3) 只对重点：AI 巨头（如 Anthropic）合作且包含算力/云/基础设施信号 的消息推手机

说明：
- 不依赖付费新闻 API / LLM
- 去重依赖 link（URL），并在本地文件保存一段时间避免重复推送
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from app.config import DATA_DIR, NEWS_DEDUP_HOURS, NEWS_MONITOR_INTERVAL_MIN, NEWS_RSS_QUERIES
from app.notifier import notify
from app.utils import now_beijing

logger = logging.getLogger(__name__)


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# 关键词（尽量用英文，Google News 搜索更稳定）
AI_KW = [
    "ai",
    "artificial intelligence",
    "llm",
    "large language model",
    "model deployment",
    "inference",
    "training",
    "foundation model",
]

COOP_KW = [
    "cooperation",
    "collaboration",
    "partnership",
    "agreement",
    "deal",
    "contract",
    "memorandum",
    "framework agreement",
]

INFRA_KW = [
    "compute",
    "gpu",
    "datacenter",
    "data center",
    "cloud",
    "infrastructure",
    "servers",
    "capacity",
    "training",
    "inference",
]

# 重点 AI 巨头（你可继续加）
AI_BIG_KW = [
    "anthropic",
    "openai",
    "google deepmind",
    "meta ai",
    "xai",
    "microsoft",
    "amazon web services",
    "aws",
    "google cloud",
    "ibm watson",
]

MONEY_KW = ["$", "billion", "million", "usd", "亿美元", "百万", "十亿", "trillion"]


# 解析/清洗
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


RSS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def strip_html(text: str) -> str:
    text = TAG_RE.sub(" ", text or "")
    text = html.unescape(text)
    text = SPACE_RE.sub(" ", text).strip()
    return text


def now_iso() -> str:
    return now_beijing().isoformat(timespec="seconds")


def _dedup_path() -> Path:
    return Path(DATA_DIR) / "news_sent.json"


def _load_sent() -> dict[str, float]:
    path = _dedup_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): float(v) for k, v in data.items()}
    except Exception as exc:
        logger.warning("load sent failed: %s", exc)
    return {}


def _save_sent(sent: dict[str, float]) -> None:
    path = _dedup_path()
    try:
        path.write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.warning("save sent failed: %s", exc)


def _match_score(text_l: str, kws: list[str]) -> int:
    score = 0
    for kw in kws:
        if kw in text_l:
            score += 1
    return score


def classify_item(title: str, desc: str) -> dict[str, Any]:
    text = (title + " " + desc).lower()
    ai_hits = _match_score(text, AI_KW)
    coop_hits = _match_score(text, COOP_KW)
    infra_hits = _match_score(text, INFRA_KW)
    big_hits = _match_score(text, AI_BIG_KW)
    money_hits = _match_score(text, MONEY_KW)

    # 基础过滤：必须命中 AI + 合作（否则太泛）
    if ai_hits <= 0 or coop_hits <= 0:
        return {"keep": False}

    # 得分：大体上“合作 + 算力/云基础设施”越多越靠前
    score = ai_hits * 3 + coop_hits * 4 + infra_hits * 2 + big_hits * 6 + money_hits * 1

    # 重点推送：AI 巨头 + 合作 + 算力/云/基础设施（至少 1 个）
    is_big_deal = big_hits > 0 and infra_hits > 0 and coop_hits > 0 and ai_hits > 0

    return {
        "keep": True,
        "score": score,
        "ai_hits": ai_hits,
        "coop_hits": coop_hits,
        "infra_hits": infra_hits,
        "big_hits": big_hits,
        "money_hits": money_hits,
        "priority": "phone" if is_big_deal else "web",
    }


@dataclass
class NewsItem:
    title: str
    link: str
    pub: str
    desc: str
    priority: str
    score: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "pub": self.pub,
            "desc": self.desc,
            "priority": self.priority,
            "score": self.score,
        }


async def _fetch_rss(url: str) -> list[dict[str, str]]:
    async with httpx.AsyncClient(headers=RSS_HEADERS, timeout=20, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        xml = resp.text
    root = ET.fromstring(xml)
    channel = root.find("channel")
    if channel is None:
        return []
    items: list[dict[str, str]] = []
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if title and link:
            items.append({"title": title, "link": link, "pub": pub, "desc": desc})
    return items


async def check_news_once() -> dict[str, Any]:
    """
    运行一次：抓取候选 → 分类 → 更新内存/返回结果
    高优先级（phone）才 push，并写入 dedup。
    """
    # 先从 RSS 拉候选
    rss_sources = []
    for q in NEWS_RSS_QUERIES:
        rss_sources.append(GOOGLE_NEWS_RSS.format(q=quote(q)))

    fetched: list[dict[str, str]] = []
    for url in rss_sources:
        try:
            fetched.extend(await _fetch_rss(url))
        except Exception as exc:
            logger.warning("rss fetch failed %s: %s", url, exc)

    # dedup（本轮）
    seen_links: set[str] = set()
    unique: list[dict[str, str]] = []
    for it in fetched:
        if it["link"] in seen_links:
            continue
        seen_links.add(it["link"])
        unique.append(it)

    sent = _load_sent()
    # 清理旧记录
    cutoff = time.time() - NEWS_DEDUP_HOURS * 3600
    sent = {k: v for k, v in sent.items() if v >= cutoff}

    results: list[NewsItem] = []
    pushed = 0

    for it in unique:
        title = it["title"]
        desc = strip_html(it["desc"])
        cls = classify_item(title, desc)
        if not cls.get("keep"):
            continue

        priority = cls.get("priority", "web")
        score = int(cls.get("score", 0))
        pub = it["pub"] or now_iso()
        item = NewsItem(
            title=title,
            link=it["link"],
            pub=pub,
            desc=desc[:500],
            priority=priority,
            score=score,
        )

        # phone：只推一次
        if priority == "phone":
            item_id = it["link"]
            if item_id not in sent:
                content = (
                    f"<b>AI合作/算力协议重点消息</b><br>"
                    f"时间：{pub}<br><br>"
                    f"{title}<br><br>"
                    f"{item.desc}<br><br>"
                    f"<a href=\"{item.link}\">原文链接</a>"
                )
                notify_title = f"[AI重点] {title[:70]}"
                push_res = await notify(notify_title, content)
                logger.info("news notify result: %s", push_res)
                any_ok = any(bool(v) for v in (push_res or {}).values())
                if any_ok:
                    sent[item_id] = time.time()
                    pushed += 1

        results.append(item)

    # 持久化去重表
    _save_sent(sent)

    # 只返回 top（页面更好展示）
    results.sort(key=lambda x: x.score, reverse=True)
    return {
        "success": True,
        "fetched": len(unique),
        "kept": len(results),
        "pushed": pushed,
        "items": [r.to_dict() for r in results[:25]],
        "ts": now_iso(),
    }


# 在进程内缓存最近一次结果，供页面展示
_LATEST: dict[str, Any] | None = None


async def update_latest() -> dict[str, Any]:
    global _LATEST
    data = await check_news_once()
    _LATEST = data
    return data


def get_latest() -> dict[str, Any] | None:
    return _LATEST

