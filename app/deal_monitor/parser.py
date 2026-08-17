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

    # 标题模式：X Signs ... with Y / X and Y Announce
    patterns = [
        r"(?P<a>.+?)\s+(?:signs?|signed|enters?|announces?)\s+.+\s+with\s+(?P<b>.+?)(?:\.|$)",
        r"(?P<a>.+?)\s+and\s+(?P<b>.+?)\s+(?:announce|sign|enter|partner)",
    ]
    for pat in patterns:
        m = re.search(pat, headline, re.I)
        if m:
            a_name, b_name = m.group("a").strip(), m.group("b").strip()
            ea = _match_entity(a_name, ents) or _match_entity_in_text(a_name, ents)
            eb = _match_entity(b_name, ents) or _match_entity_in_text(b_name, ents)
            if ea and eb and ea.key != eb.key:
                return ea, eb

    # 默认：取识别到的前两个不同实体
    if len(ents) >= 2:
        return ents[0], ents[1]
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
