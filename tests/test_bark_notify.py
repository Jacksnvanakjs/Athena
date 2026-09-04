"""Bark 推送通道。"""

import asyncio
from unittest.mock import AsyncMock, patch

from app import notifier


def test_send_bark_posts_json():
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"code":200}'
    mock_resp.json = lambda: {"code": 200}

    mock_client = AsyncMock()
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch.object(notifier, "BARK_DEVICE_KEY", "testkey123"):
        with patch.object(notifier, "BARK_SERVER_URL", "https://api.day.app"):
            with patch.object(notifier.httpx, "AsyncClient", return_value=mock_client):
                ok = asyncio.run(notifier.send_bark("标题", "正文内容"))
    assert ok is True
    mock_client.post.assert_awaited()
    args, kwargs = mock_client.post.await_args
    assert args[0] == "https://api.day.app/push"
    assert kwargs["json"]["device_key"] == "testkey123"
    assert kwargs["json"]["title"] == "标题"
    assert kwargs["json"]["body"] == "正文内容"


def test_notify_includes_bark():
    with patch.object(notifier, "PUSHPLUS_TOKEN", ""):
        with patch.object(notifier, "BARK_DEVICE_KEY", "k"):
            with patch.object(notifier, "send_bark", AsyncMock(return_value=True)) as bark:
                results = asyncio.run(notifier.notify("t", "c"))
    assert results == {"bark": True}
    bark.assert_awaited_once()
