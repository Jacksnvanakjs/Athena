"""过滤不应展示/入库的测试或本地占位 URL。"""

from __future__ import annotations

from urllib.parse import urlparse

# 明确测试域名/路径前缀
_BLOCKED_HOSTS = frozenset(
    {
        "test",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "example.com",
        "example.org",
        "example.net",
    }
)

_BLOCKED_HOST_SUFFIXES = (
    ".test",
    ".localhost",
    ".invalid",
)


def is_test_source_url(url: str | None) -> bool:
    """测试稿、本地占位链接不应入库或在页面展示。"""
    raw = (url or "").strip()
    if not raw:
        return True
    lower = raw.lower()
    if lower.startswith("file:") or lower.startswith("about:"):
        return True
    try:
        parsed = urlparse(raw)
    except Exception:
        return True
    host = (parsed.hostname or "").lower()
    if not host:
        return True
    if host in _BLOCKED_HOSTS:
        return True
    if any(host.endswith(suf) for suf in _BLOCKED_HOST_SUFFIXES):
        return True
    if host.startswith("127.") or host.startswith("10.") or host.startswith("192.168."):
        return True
    path = (parsed.path or "").lower()
    if path.startswith("/test/") or "/test/" in path:
        return True
    return False
