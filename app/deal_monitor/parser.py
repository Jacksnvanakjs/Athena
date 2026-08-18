"""标题/正文解析与合作双方推断。"""

from __future__ import annotations

import re

from app.deal_monitor.entities import Entity


def infer_partnership_pair(
    headline: str,
    summary: str,
    entities: list[Entity],
) -> tuple[Entity, Entity] | None:
    if len(entities) < 2:
        return None

    text = f"{headline} {summary}"
    unique: dict[str, Entity] = {}
    for e in entities:
        unique[e.key] = e
    ents = list(unique.values())
    if len(ents) < 2:
        return None

    text = f"{headline} {summary}"

    # 标题/摘要模式：X Signs ... with Y / X partners with Y ...
    patterns = [
        r"(?P<a>.+?)\s+(?:signs?|signed|enters?|announces?)\s+.+\s+with\s+(?P<b>.+?)(?:\.|$)",
        # X and Y Announce / X and Y partner ...
        r"(?P<a>.+?)\s+and\s+(?P<b>.+?)\s+(?:announce|announced|sign|enter|partner)",
        # 常见通稿结构：X partners with Y / X partnered with Y
        r"(?P<a>.+?)\s+(?:partners?|partnered)\s+with\s+(?P<b>.+?)(?:\.|$)",
        # 常见通稿结构：X collaborates with Y / X collaborated with Y
        r"(?P<a>.+?)\s+(?:collaborates?|collaborated)\s+with\s+(?P<b>.+?)(?:\.|$)",
        # 常见通稿结构：X enters into ... partnership with Y
        r"(?P<a>.+?)\s+(?:enters?|enter)\s+(?:into\s+)?(?:partnership|agreement|contract)\s+with\s+(?P<b>.+?)(?:\.|$)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            a_name, b_name = m.group("a").strip(), m.group("b").strip()
            if len(a_name) > 60 or len(b_name) > 60:
                continue
            ea = _match_entity(a_name, ents) or _match_entity_in_text(a_name, ents)
            eb = _match_entity(b_name, ents) or _match_entity_in_text(b_name, ents)
            if ea and eb and ea.key != eb.key:
                return ea, eb

    # 默认：取识别到的前两个不同实体
    if len(ents) >= 2:
        return ents[0], ents[1]
    return None


def infer_partnership_pair_text(
    headline: str,
    summary: str,
) -> tuple[str, str] | None:
    """当种子库无法映射实体时，先从标题提取“双方名称片段”。"""

    def _trim_fragment(s: str) -> str:
        s = s.strip()
        if not s:
            return s
        # 截断常见从句（尽量保留公司名主体）
        m = re.search(r"\b(?:to|for|on|in|under|with)\b\s+", s, re.I)
        if m:
            return s[: m.start()].strip()
        return s

    text = f"{headline} {summary}"

    patterns = [
        # X Signs ... with Y / X Signed ... with Y
        r"(?P<a>.+?)\s+(?:signs?|signed|enters?|announces?)\s+.+?\s+with\s+(?P<b>.+?)(?:\.|$)",
        # X and Y Announce / X and Y partner ...
        r"(?P<a>.+?)\s+and\s+(?P<b>.+?)\s+(?:announce|announced|sign|enter|partner)",
        # X partners with Y / X partnered with Y
        r"(?P<a>.+?)\s+(?:partners?|partnered)\s+with\s+(?P<b>.+?)(?:\.|$)",
        # X collaborates with Y / X collaborated with Y
        r"(?P<a>.+?)\s+(?:collaborates?|collaborated)\s+with\s+(?P<b>.+?)(?:\.|$)",
        # X enters into ... with Y
        r"(?P<a>.+?)\s+(?:enters?|enter)\s+(?:into\s+)?(?:partnership|agreement|contract)\s+with\s+(?P<b>.+?)(?:\.|$)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        a_name = _trim_fragment(m.group("a"))
        b_name = _trim_fragment(m.group("b"))
        if (
            a_name
            and b_name
            and a_name.lower() != b_name.lower()
            and len(a_name) <= 60
            and len(b_name) <= 60
        ):
            return a_name, b_name
    return None


def _match_entity(fragment: str, entities: list[Entity]) -> Entity | None:
    frag = fragment.lower()
    for e in entities:
        if e.name.lower() in frag or (e.ticker and e.ticker.lower() in frag):
            return e
    return None


def _match_entity_in_text(fragment: str, entities: list[Entity]) -> Entity | None:
    frag = fragment.lower()
    for e in entities:
        if e.name.lower() in frag:
            return e
    return None
