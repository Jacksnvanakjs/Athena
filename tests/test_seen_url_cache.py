from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import DealSeenUrl
from app.seen_url_cache import SeenUrlCache


def test_seen_url_cache_skips_known_urls():
    engine = create_engine("sqlite://")
    DealSeenUrl.__table__.create(engine)
    session_factory = sessionmaker(bind=engine)
    cache = SeenUrlCache()
    with session_factory() as db:
        db.add(
            DealSeenUrl(
                source_url="https://a.example/1",
                headline_hash="h",
                seen_at=datetime(2026, 9, 20, 1, 0, 0),
                llm_relevant=False,
            )
        )
        db.commit()
        seen, unseen = cache.partition(
            db, DealSeenUrl, ["https://a.example/1", "https://b.example/2", ""]
        )
        assert seen == {"https://a.example/1"}
        assert unseen == {"https://b.example/2"}

        statements: list[str] = []

        def _capture(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _capture)
        seen2, unseen2 = cache.partition(db, DealSeenUrl, ["https://a.example/1"])
        event.remove(engine, "before_cursor_execute", _capture)
        assert seen2 == {"https://a.example/1"}
        assert unseen2 == set()
        assert statements == []
