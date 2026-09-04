"""Turso 多节点探测：不回退 SQLite。"""

from unittest.mock import patch

import app.database as database


def test_try_startup_db_all_fail_stays_turso_not_ready():
    database._db_ready.clear()
    database._active_backend = "turso"
    with patch.object(database, "USE_TURSO", True):
        with patch.object(database, "TURSO_AUTH_TOKEN", "tok"):
            with patch.object(database, "TURSO_DATABASE_URLS", ["libsql://a.turso.io", "libsql://b.turso.io"]):
                with patch.object(database, "TURSO_CONNECT_RETRIES", 1):
                    with patch.object(database, "_turso_reachable_in_subprocess", return_value=False):
                        with patch.object(database, "reset_engine"):
                            ok = database.try_startup_db(timeout_sec=0.2)
    assert ok is False
    assert database.is_db_ready() is False
    assert database.get_db_backend() == "turso"


def test_try_startup_db_all_fail_keeps_ready_if_already_up():
    """周期探测失败时不清掉已就绪，避免页面间歇断库。"""
    database._db_ready.set()
    database._active_backend = "turso"
    with patch.object(database, "USE_TURSO", True):
        with patch.object(database, "TURSO_AUTH_TOKEN", "tok"):
            with patch.object(database, "TURSO_DATABASE_URLS", ["libsql://a.turso.io"]):
                with patch.object(database, "TURSO_CONNECT_RETRIES", 1):
                    with patch.object(database, "_turso_reachable_in_subprocess", return_value=False):
                        with patch.object(database, "reset_engine"):
                            ok = database.try_startup_db(timeout_sec=0.2)
    assert ok is True
    assert database.is_db_ready() is True
    assert database.get_db_backend() == "turso"


def test_try_startup_db_success_marks_ready():
    database._db_ready.clear()
    with patch.object(database, "USE_TURSO", True):
        with patch.object(database, "TURSO_AUTH_TOKEN", "tok"):
            with patch.object(database, "TURSO_DATABASE_URLS", ["libsql://a.turso.io"]):
                with patch.object(database, "TURSO_CONNECT_RETRIES", 1):
                    with patch.object(database, "_turso_reachable_in_subprocess", return_value=True):
                        with patch.object(database, "reset_engine") as reset:
                            with patch.object(database, "_sync_embedded_replica"):
                                ok = database.try_startup_db(timeout_sec=2.0)
    assert ok is True
    assert database.is_db_ready() is True
    assert database.get_db_backend() == "turso"
    reset.assert_called()


def test_try_startup_db_tries_fallback_url():
    database._db_ready.clear()
    calls: list[str] = []

    def ping(url: str, timeout: float) -> bool:
        calls.append(url)
        return url.endswith("b.turso.io")

    with patch.object(database, "USE_TURSO", True):
        with patch.object(database, "TURSO_AUTH_TOKEN", "tok"):
            with patch.object(
                database,
                "TURSO_DATABASE_URLS",
                ["libsql://a.turso.io", "libsql://b.turso.io"],
            ):
                with patch.object(database, "TURSO_CONNECT_RETRIES", 1):
                    with patch.object(database, "_turso_reachable_in_subprocess", side_effect=ping):
                        with patch.object(database, "reset_engine"):
                            with patch.object(database, "_sync_embedded_replica"):
                                ok = database.try_startup_db(timeout_sec=2.0)
    assert ok is True
    assert calls == ["libsql://a.turso.io", "libsql://b.turso.io"]
