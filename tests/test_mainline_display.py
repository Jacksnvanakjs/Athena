"""主线展示：5D 确认 vs 1D 脉搏、近2周轮动压缩。"""

from datetime import date

from app.ai_mainline.pipeline import (
    _compute_pulse_1d,
    _display_summary,
    _finalize_payload,
    _is_1d_weak,
    _pulse_history_with_today,
    _rotation_runs,
    _short_rotation_eligible,
    _sync_primary_from_themes,
)


def test_rotation_runs_compress_consecutive():
    items = [
        {"trade_date": "2026-09-08", "primary_key": "optical", "primary_name": "光通信", "status": "emerging"},
        {"trade_date": "2026-09-09", "primary_key": "optical", "primary_name": "光通信", "status": "confirmed"},
        {"trade_date": "2026-09-10", "primary_key": "ai_security", "primary_name": "AI安全/身份", "status": "emerging"},
        {"trade_date": "2026-09-11", "primary_key": "ai_security", "primary_name": "AI安全/身份", "status": "confirmed"},
        {"trade_date": "2026-09-12", "primary_key": None, "primary_name": None, "status": "no_mainline"},
    ]
    runs = _rotation_runs(items)
    assert len(runs) == 3
    assert runs[0]["name"] == "光通信" and runs[0]["days"] == 2 and runs[0]["status"] == "confirmed"
    assert runs[1]["name"] == "AI安全/身份" and runs[1]["days"] == 2
    assert runs[2]["key"] is None and runs[2]["name"] == "暂无明确主线"


def test_pulse_history_overlays_today_1d(monkeypatch):
    monkeypatch.setattr(
        "app.ai_mainline.pipeline.history_pulse_1d",
        lambda days=10: [
            {
                "trade_date": "2026-09-17",
                "primary_key": "memory",
                "primary_name": "存储/HBM链",
                "status": "pulse",
                "ret_1d": 1.2,
            },
            {
                "trade_date": "2026-09-18",
                "primary_key": "ai_sec",
                "primary_name": "AI安全/身份",
                "status": "pulse",
                "ret_1d": 0.4,
            },
        ],
    )
    monkeypatch.setattr("app.ai_mainline.pipeline._today_et", lambda: date(2026, 9, 18))
    monkeypatch.setattr("app.ai_mainline.pipeline._live_1d_active", lambda phase=None: True)
    hist = _pulse_history_with_today(
        {
            "1d_pending": False,
            "trade_date": "2026-09-17",
            "pulse_1d": {
                "key": "software",
                "name": "软件",
                "ret_1d": 2.7,
                "n_valid": 8,
                "n_up": 8,
                "breadth": 1.0,
            },
        }
    )
    assert [h["primary_key"] for h in hist] == ["memory", "software"]
    runs = _rotation_runs(hist)
    assert runs[-1]["name"] == "软件"
    assert runs[-1]["ret_1d"] == 2.7
    assert runs[-1]["days"] == 1


def test_short_rotation_requires_full_breadth_and_min_1d():
    assert _short_rotation_eligible(
        {"ret_1d": 2.0, "n_valid": 5, "n_up": 5, "breadth": 1.0}
    )
    assert not _short_rotation_eligible(
        {"ret_1d": 3.5, "n_valid": 5, "n_up": 4, "breadth": 0.8}
    )
    assert not _short_rotation_eligible(
        {"ret_1d": 1.9, "n_valid": 6, "n_up": 6, "breadth": 1.0}
    )
    assert not _short_rotation_eligible(
        {"ret_1d": 4.0, "n_valid": 2, "n_up": 2, "breadth": 1.0}
    )


def test_pulse_history_skips_today_if_short_filter_fails(monkeypatch):
    monkeypatch.setattr(
        "app.ai_mainline.pipeline.history_pulse_1d",
        lambda days=10: [
            {
                "trade_date": "2026-09-18",
                "primary_key": "memory",
                "primary_name": "存储/HBM链",
                "status": "pulse",
                "ret_1d": 2.4,
            },
        ],
    )
    monkeypatch.setattr("app.ai_mainline.pipeline._today_et", lambda: date(2026, 9, 18))
    monkeypatch.setattr("app.ai_mainline.pipeline._live_1d_active", lambda phase=None: True)
    hist = _pulse_history_with_today(
        {
            "1d_pending": False,
            "pulse_1d": {
                "key": "software",
                "name": "软件",
                "ret_1d": 3.1,
                "n_valid": 8,
                "n_up": 7,
                "breadth": 0.875,
            },
        }
    )
    assert hist[-1]["primary_key"] is None
    assert hist[-1]["primary_name"] == "暂无明确短线"


def test_pulse_1d_and_weak_flag():
    themes = [
        {"key": "a", "name": "A", "ret_1d": 0.5, "n_valid": 6, "n_up": 1, "breadth": 0.16},
        {"key": "b", "name": "B", "ret_1d": 2.1, "n_valid": 5, "n_up": 4, "breadth": 0.8},
        {"key": "c", "name": "C", "ret_1d": 9.0, "n_valid": 2, "n_up": 2, "breadth": 1.0},
    ]
    pulse = _compute_pulse_1d(themes)
    assert pulse["key"] == "b"
    assert _is_1d_weak({"n_valid": 6, "n_up": 0, "breadth": 0.0}) is True
    assert _is_1d_weak({"n_valid": 6, "n_up": 3, "breadth": 0.5}) is False


def test_sync_primary_and_summary_separates_1d():
    payload = {
        "status": "confirmed",
        "primary": {
            "key": "ai_security",
            "name": "AI安全/身份",
            "status": "confirmed",
            "ret_5d": 4.2,
            "rel_5d": 2.1,
            "n_up": 5,
            "n_valid": 6,
            "breadth": 0.83,
        },
        "secondary": {"key": "optical", "name": "光通信"},
        "themes": [
            {
                "key": "ai_security",
                "name": "AI安全/身份",
                "ret_1d": -0.8,
                "n_up": 0,
                "n_valid": 6,
                "breadth": 0.0,
            },
            {
                "key": "optical",
                "name": "光通信",
                "ret_1d": 1.2,
                "n_up": 4,
                "n_valid": 5,
                "breadth": 0.8,
            },
        ],
        "success": True,
    }
    _sync_primary_from_themes(payload)
    assert payload["primary"]["n_up"] == 0
    payload["pulse_1d"] = _compute_pulse_1d(payload["themes"])
    text = _display_summary(payload)
    assert "5日主线：AI安全/身份" in text
    assert "今日1D广度：上涨 0/6" in text
    assert "偏弱" in text
    assert "今日1D脉搏：光通信" in text


def test_finalize_sets_basis_without_db_crash(monkeypatch):
    monkeypatch.setattr(
        "app.ai_mainline.pipeline.history_primary",
        lambda days=14: [
            {
                "trade_date": "2026-09-18",
                "primary_key": "ai_security",
                "primary_name": "AI安全/身份",
                "status": "confirmed",
            }
        ],
    )
    monkeypatch.setattr(
        "app.ai_mainline.pipeline.history_pulse_1d",
        lambda days=10: [
            {
                "trade_date": "2026-09-17",
                "primary_key": "memory",
                "primary_name": "存储/HBM链",
                "status": "pulse",
                "ret_1d": 1.2,
                "n_valid": 4,
            },
            {
                "trade_date": "2026-09-18",
                "primary_key": "optical",
                "primary_name": "光通信",
                "status": "pulse",
                "ret_1d": 2.0,
                "n_valid": 5,
            },
        ],
    )
    monkeypatch.setattr("app.ai_mainline.pipeline._market_phase", lambda now=None: "rth")
    monkeypatch.setattr("app.ai_mainline.pipeline._live_1d_active", lambda phase=None: True)
    out = _finalize_payload(
        {
            "success": True,
            "status": "confirmed",
            "primary": {
                "key": "ai_security",
                "name": "AI安全/身份",
                "status": "confirmed",
                "ret_5d": 3.0,
                "rel_5d": 1.5,
            },
            "themes": [
                {
                    "key": "ai_security",
                    "name": "AI安全/身份",
                    "ret_1d": -1.0,
                    "n_up": 0,
                    "n_valid": 6,
                    "breadth": 0.0,
                }
            ],
        }
    )
    assert out["mainline_basis"] == "rel_5d"
    assert out["rotation_14d"][0]["name"] == "AI安全/身份"
    assert out.get("rotation_1d")
    assert any(r.get("name") == "光通信" for r in out["rotation_1d"])
    # 盘中未叠加上今日报价时，不能沿用快照 1D
    assert out.get("1d_pending") is True
    assert out["themes"][0]["ret_1d"] is None
    assert not out.get("1d_hold")


def test_pending_refresh_keeps_last_live_1d(monkeypatch):
    monkeypatch.setattr("app.ai_mainline.pipeline._live_1d_active", lambda phase=None: True)
    monkeypatch.setattr("app.ai_mainline.pipeline._market_phase", lambda now=None: "rth")
    monkeypatch.setattr("app.ai_mainline.pipeline.history_primary", lambda days=14: [])
    monkeypatch.setattr("app.ai_mainline.pipeline.history_pulse_1d", lambda days=10: [])
    from app.ai_mainline.pipeline import _finalize_payload, _mark_1d_pending

    live = {
        "success": True,
        "status": "confirmed",
        "live_1d": True,
        "themes": [
            {
                "key": "memory",
                "name": "存储/HBM链",
                "ret_1d": 2.4,
                "n_up": 4,
                "n_valid": 4,
                "breadth": 1.0,
                "members": [{"symbol": "MU", "ret_1d": 3.1}],
            }
        ],
        "primary": {"key": "memory", "name": "存储/HBM链", "status": "confirmed"},
    }
    pending = _mark_1d_pending(live, note="正在拉取最新1D")
    assert pending["1d_pending"] is True
    assert pending["live_1d"] is False
    assert pending["1d_hold"] is True
    assert pending["themes"][0]["ret_1d"] == 2.4
    assert pending["themes"][0]["members"][0]["ret_1d"] == 3.1

    out = _finalize_payload(pending)
    assert out["themes"][0]["ret_1d"] == 2.4
    assert out["1d_pending"] is True
    assert out["1d_hold"] is True


def test_closed_finalize_holds_snapshot_1d(monkeypatch):
    """休市未叠 live 时保留快照 1D，禁止整列「—」。"""
    monkeypatch.setattr("app.ai_mainline.pipeline._live_1d_active", lambda phase=None: True)
    monkeypatch.setattr("app.ai_mainline.pipeline._market_phase", lambda now=None: "closed")
    monkeypatch.setattr("app.ai_mainline.pipeline.history_primary", lambda days=14: [])
    monkeypatch.setattr("app.ai_mainline.pipeline.history_pulse_1d", lambda days=10: [])
    from app.ai_mainline.pipeline import _finalize_payload

    out = _finalize_payload(
        {
            "success": True,
            "status": "emerging",
            "live_1d": False,
            "trade_date": "2026-10-09",
            "themes": [
                {
                    "key": "memory",
                    "name": "存储/HBM链",
                    "ret_1d": 1.5,
                    "members": [{"symbol": "MU", "ret_1d": 2.0}],
                }
            ],
            "primary": {"key": "memory", "name": "存储/HBM链"},
        }
    )
    assert out["1d_pending"] is True
    assert out["1d_hold"] is True
    assert out["themes"][0]["ret_1d"] == 1.5
    assert out["themes"][0]["members"][0]["ret_1d"] == 2.0
    # 必须有可展示时间，不能只剩「—」
    assert out.get("data_time_daily_bj") or out.get("updated_bj")


def test_backfill_1d_times_from_member_post_stamp(monkeypatch):
    monkeypatch.setattr("app.ai_mainline.pipeline._market_phase", lambda now=None: "closed")
    from app.ai_mainline.pipeline import _backfill_1d_times_from_members, _ensure_display_times

    out = _ensure_display_times(
        _backfill_1d_times_from_members(
            {
                "trade_date": "2026-10-09",
                "data_time_1d_bj": None,
                "data_time_1d_et": None,
                "themes": [
                    {
                        "key": "t",
                        "members": [
                            {
                                "symbol": "NVDA",
                                "ret_1d": -0.5,
                                "quote_time": "2026-10-10 08:00:00",
                                "quote_time_et": "2026-10-09 20:00:00 EDT",
                            }
                        ],
                    }
                ],
            }
        )
    )
    assert out["data_time_1d_bj"] == "2026-10-10 08:00:00"
    assert "20:00" in (out.get("data_time_1d_et") or "")
    assert out.get("data_time_daily_bj")
