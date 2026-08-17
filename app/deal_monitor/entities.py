"""公司名 → ticker / 未上市锚点映射。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.database import EntityAlias
from app.deal_monitor.config import ENTITIES_SEED_FILE
from app.utils import now_beijing

logger = logging.getLogger(__name__)


@dataclass
class Entity:
    name: str
    ticker: str | None = None
    unlisted_id: str | None = None
    tier: str = "UNKNOWN"
    market_cap_usd: float | None = None

    @property
    def key(self) -> str:
        if self.ticker:
            return self.ticker
        if self.unlisted_id:
            return f"unlisted:{self.unlisted_id}"
        return f"unknown:{self.name}"

    @property
    def is_unknown(self) -> bool:
        return not self.ticker and not self.unlisted_id


class EntityRegistry:
    def __init__(self):
        self._aliases: list[tuple[str, str | None, str | None]] = []
        self._t0_listed: set[str] = set()
        self._loaded = False

    def load_seed(self) -> None:
        if self._loaded:
            return
        if not ENTITIES_SEED_FILE.is_file():
            logger.warning("entities_seed.json 不存在: %s", ENTITIES_SEED_FILE)
            self._loaded = True
            return
        data = json.loads(ENTITIES_SEED_FILE.read_text(encoding="utf-8"))
        for item in data.get("aliases", []):
            ticker = item.get("ticker")
            for name in item.get("names", []):
                self._aliases.append((name.lower(), ticker, None))
        for item in data.get("unlisted_anchors", []):
            uid = item.get("id")
            for name in item.get("names", []):
                self._aliases.append((name.lower(), None, uid))
        self._t0_listed = {t.upper() for t in data.get("t0_listed_tickers", [])}
        self._aliases.sort(key=lambda x: len(x[0]), reverse=True)
        self._loaded = True

    def sync_to_db(self, db: Session) -> None:
        self.load_seed()
        now = now_beijing()
        existing_names = {row.name for row in db.query(EntityAlias.name).all()}
        for name, ticker, uid in self._aliases:
            if name in existing_names:
                continue
            db.add(
                EntityAlias(
                    name=name,
                    ticker=ticker,
                    unlisted_id=uid,
                    updated_at=now,
                )
            )
            existing_names.add(name)
        db.commit()

    def is_t0_listed_seed(self, ticker: str | None) -> bool:
        self.load_seed()
        return bool(ticker and ticker.upper() in self._t0_listed)

    def extract_entities(self, text: str) -> list[Entity]:
        self.load_seed()
        norm = text.lower()
        found: dict[str, Entity] = {}
        for alias, ticker, uid in self._aliases:
            if alias in norm:
                key = ticker or uid or alias
                if key not in found:
                    display = alias
                    for orig in self._aliases:
                        oname, ot, ou = orig
                        if (ticker and ot == ticker) or (uid and ou == uid):
                            if len(oname) > len(display):
                                display = oname
                    found[key] = Entity(
                        name=display.title() if display.islower() else display,
                        ticker=ticker,
                        unlisted_id=uid,
                    )
        return list(found.values())


registry = EntityRegistry()
