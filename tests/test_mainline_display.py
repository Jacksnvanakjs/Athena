"""主线展示：5D 确认 vs 1D 脉搏、近2周轮动压缩。"""

from app.ai_mainline.pipeline import (
    _compute_pulse_1d,
    _display_summary,
    _finalize_payload,
    _is_1d_weak,
    _rotation_runs,
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
    assert out["pulse_1d_weak"] is True
    assert out["rotation_14d"][0]["name"] == "AI安全/身份"
