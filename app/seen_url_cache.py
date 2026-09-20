"""已见 URL 的进程内缓存。

热路径只查内存。缓存未命中时按主键点查本批 URL，不再每轮扫整张 seen 表。
"""

from __future__ import annotations

import threading
import time

_IN_CHUNK = 400


class SeenUrlCache:
    def __init__(self) -> None:
        self._urls: set[str] | None = None
        self._lock = threading.Lock()

    def add(self, url: str) -> None:
        url = (url or "").strip()
        if not url:
            return
        with self._lock:
            if self._urls is None:
                self._urls = set()
            self._urls.add(url)

    def add_many(self, urls) -> None:
        for url in urls:
            self.add(url)

    def partition(self, db, model, urls: list[str]) -> tuple[set[str], set[str]]:
        """返回 (已见, 未见)。已在内存中的 URL 不再访问数据库。"""
        cleaned: list[str] = []
        seen_in: set[str] = set()
        for raw in urls:
            url = (raw or "").strip()
            if not url or url in seen_in:
                continue
            seen_in.add(url)
            cleaned.append(url)
        with self._lock:
            if self._urls is None:
                self._urls = set()
            known = {url for url in cleaned if url in self._urls}
            unknown = [url for url in cleaned if url not in self._urls]
        if not unknown:
            return known, set()
        found: set[str] = set()
        for offset in range(0, len(unknown), _IN_CHUNK):
            chunk = unknown[offset : offset + _IN_CHUNK]
            rows = db.query(model.source_url).filter(model.source_url.in_(chunk)).all()
            for row in rows:
                found.add(row[0])
        with self._lock:
            self._urls.update(found)
        return known | found, set(unknown) - found


class TtlBox:
    """短 TTL 缓存，用来挡住轮询里的重复聚合。"""

    def __init__(self, ttl_sec: float) -> None:
        self.ttl = ttl_sec
        self._at = 0.0
        self._value = None
        self._lock = threading.Lock()

    def get(self, loader):
        now = time.monotonic()
        with self._lock:
            if self._value is not None and now - self._at < self.ttl:
                return self._value
        value = loader()
        with self._lock:
            self._at = time.monotonic()
            self._value = value
        return value


deal_seen_urls = SeenUrlCache()
nvda_seen_urls = SeenUrlCache()
