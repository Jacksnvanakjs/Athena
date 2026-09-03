"""多源级联：按顺序尝试，返回首个有效结果。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class SourceResult:
    value: Any
    source: str


async def first_success(
    kind: str,
    providers: list[tuple[str, Callable[[], Awaitable[T | None]]]],
    *,
    is_ok: Callable[[T], bool] | None = None,
    context: str = "",
) -> SourceResult | None:
    """依次调用 providers；``is_ok`` 判定有效（默认非 None / 非空容器）。"""

    def _default_ok(v: T) -> bool:
        if v is None:
            return False
        if isinstance(v, (list, tuple, dict, set, str)) and len(v) == 0:
            return False
        return True

    check = is_ok or _default_ok
    label = f"{kind}:{context}" if context else kind
    for name, factory in providers:
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
