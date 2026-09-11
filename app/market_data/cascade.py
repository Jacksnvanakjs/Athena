"""多源级联：按顺序尝试，返回首个有效结果；支持轮动起点。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_rotate_lock = threading.Lock()
_rotate_counters: dict[str, int] = {}


@dataclass
class SourceResult:
    value: Any
    source: str


def rotate_providers(
    kind: str,
    providers: Sequence[tuple[str, Callable[[], Awaitable[T | None]]]],
) -> list[tuple[str, Callable[[], Awaitable[T | None]]]]:
    """每次调用轮动起点，避免测试时总打同一个源。"""
    if not providers:
        return []
    with _rotate_lock:
        idx = _rotate_counters.get(kind, 0) % len(providers)
        _rotate_counters[kind] = idx + 1
    items = list(providers)
    return items[idx:] + items[:idx]


async def first_success(
    kind: str,
    providers: list[tuple[str, Callable[[], Awaitable[T | None]]]],
    *,
    is_ok: Callable[[T], bool] | None = None,
    context: str = "",
    rotate: bool = True,
) -> SourceResult | None:
    """依次调用 providers；``is_ok`` 判定有效（默认非 None / 非空容器）。

    ``rotate=True``（默认）时按 kind 轮动起点，频繁测试不会总钉死第一个源。
    """

    def _default_ok(v: T) -> bool:
        if v is None:
            return False
        if isinstance(v, (list, tuple, dict, set, str)) and len(v) == 0:
            return False
        return True

    check = is_ok or _default_ok
    label = f"{kind}:{context}" if context else kind
    ordered = rotate_providers(kind, providers) if rotate else list(providers)
    if ordered and rotate:
        logger.debug(
            "多源 %s 本轮起点=%s（共 %s）",
            label,
            ordered[0][0],
            len(ordered),
        )
    for name, factory in ordered:
        try:
            value = await factory()
        except Exception as exc:
            logger.warning("多源 %s 源=%s 异常: %s", label, name, exc)
            continue
        if check(value):  # type: ignore[arg-type]
            logger.info("多源 %s 命中 %s", label, name)
            return SourceResult(value=value, source=name)
        logger.debug("多源 %s 源=%s 无有效数据", label, name)
    logger.warning("多源 %s 全部失败", label)
    return None


def next_batch_order(kind: str, names: Sequence[str]) -> list[str]:
    """给批量补缺用的源名轮动顺序。"""
    if not names:
        return []
    with _rotate_lock:
        idx = _rotate_counters.get(kind, 0) % len(names)
        _rotate_counters[kind] = idx + 1
    items = list(names)
    return items[idx:] + items[:idx]
