"""事件时间展示：published_at 为 UTC naive；fetched_at/pushed_at 为北京时间 naive。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_UTC = ZoneInfo("UTC")
_BJ = ZoneInfo("Asia/Shanghai")
_ET = ZoneInfo("America/New_York")


def format_published_at_bj(dt: datetime | None) -> str:
    """稿件发布时间（UTC 入库）→ 北京时间，不带时区备注。"""
    if not dt:
        return "—"
    aware = dt.replace(tzinfo=_UTC)
    return aware.astimezone(_BJ).strftime("%Y-%m-%d %H:%M")


def format_published_at_et(dt: datetime | None) -> str | None:
    """稿件发布时间（UTC 入库）→ 美东时间，供次要行展示。"""
    if not dt:
        return None
    aware = dt.replace(tzinfo=_UTC)
    return aware.astimezone(_ET).strftime("%Y-%m-%d %H:%M")


def format_published_at_display(dt: datetime | None) -> str:
    """兼容旧字段：等同北京时间主行。"""
    return format_published_at_bj(dt)


def format_published_at_push(dt: datetime | None) -> str:
    """推送正文用的紧凑发布时间。"""
    if not dt:
        return "—"
    aware = dt.replace(tzinfo=_UTC)
    bj = aware.astimezone(_BJ)
    et = aware.astimezone(_ET)
    return (
        f"{bj.strftime('%m-%d %H:%M')} 北京 · "
        f"{et.strftime('%m-%d %H:%M')} 美东 · "
        f"UTC {dt.strftime('%m-%d %H:%M')}"
    )


def format_beijing_at_bj(dt: datetime | None) -> str:
    """系统时间（北京时间入库）→ 北京主行。"""
    if not dt:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def format_beijing_at_et(dt: datetime | None) -> str | None:
    """系统时间（北京时间入库）→ 美东副行。"""
    if not dt:
        return None
    aware = dt.replace(tzinfo=_BJ)
    return aware.astimezone(_ET).strftime("%Y-%m-%d %H:%M")


def format_beijing_at_display(dt: datetime | None) -> str:
    """系统时间（北京时间入库）→ 北京 + 美东。"""
    if not dt:
        return "—"
    aware = dt.replace(tzinfo=_BJ)
    et = aware.astimezone(_ET)
    return (
        f"{dt.strftime('%Y-%m-%d %H:%M')} 北京 · "
        f"{et.strftime('%Y-%m-%d %H:%M')} 美东"
    )


def format_beijing_at_push(dt: datetime | None) -> str:
    if not dt:
        return "—"
    aware = dt.replace(tzinfo=_BJ)
    et = aware.astimezone(_ET)
    return (
        f"{dt.strftime('%m-%d %H:%M')} 北京 · "
        f"{et.strftime('%m-%d %H:%M')} 美东"
    )
