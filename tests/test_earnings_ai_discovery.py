"""财报 AI 动态发现单元测试（不打外网）。"""

from app.earnings_monitor.ai_discovery import _classify_sector, _is_ai_blob


def test_ai_blob_keywords():
    assert _is_ai_blob("Netskope cloud security platform")
    assert _is_ai_blob("AI semiconductor GPU")
    assert not _is_ai_blob("Regional bank holding company")


def test_classify_sector():
    assert _classify_sector("cybersecurity zero trust") == "AI_SEC"
    assert _classify_sector("GPU semiconductor chip") == "AI_SEMI"
    assert _classify_sector("data center power") == "AI_INFRA"
