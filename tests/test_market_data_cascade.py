"""多源级联单测。"""

import asyncio

from app.market_data.cascade import first_success


def test_first_success_picks_second_when_first_empty():
    async def bad():
        return []

    async def good():
        return [1, 2, 3]

    async def run():
        return await first_success(
            "test",
            [("a", bad), ("b", good)],
            is_ok=lambda v: isinstance(v, list) and len(v) >= 2,
        )

    r = asyncio.get_event_loop().run_until_complete(run())
    assert r is not None
    assert r.source == "b"
    assert r.value == [1, 2, 3]


def test_first_success_all_fail():
    async def bad():
        return None

    async def run():
        return await first_success("test", [("a", bad), ("b", bad)])

    r = asyncio.get_event_loop().run_until_complete(run())
    assert r is None
