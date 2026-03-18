import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from app.services.memory import ChatMemoryRepository, ConversationMemory


@pytest.mark.parametrize("backend", ["sqlite", "redis"])
async def test_archive_retains_verified_order_beyond_summary_window(tmp_path, backend):
    redis_url = os.getenv("TEST_MEMORY_REDIS_URL")
    if backend == "redis" and not redis_url:
        pytest.skip("set TEST_MEMORY_REDIS_URL to test real Redis Lists")
    redis = Redis.from_url(redis_url, decode_responses=True) if backend == "redis" else None
    repo = ChatMemoryRepository(tmp_path / "history.db", redis)
    repo.initialize()
    user, session = uuid4().hex, "session"
    try:
        await repo.append(user, session, {"id": "verified", "role": "assistant", "content": "订单 ORD-1001 已核实",
                                         "facts": {"order_id": "ORD-1001", "amount_cents": 32900}})
        for index in range(30):
            item = {"id": str(index), "role": "user", "content": f"第 {index} 条一般问题"}
            await repo.append(user, session, item)
        await repo.append(user, session, item)
        page, total = await repo.history(user, session, offset=21, limit=4)
        assert total == 31
        assert [row["id"] for row in page] == ["20", "21", "22", "23"]
        layered = await ConversationMemory(repo, decisions=None).build(user, session, "30")
        recent = await ConversationMemory(repo, decisions=None, variant="recent").build(user, session, "30")
        assert layered["facts"] == {"order_id": "ORD-1001", "amount_cents": 32900}
        assert recent["facts"] == {}
        assert (await repo.history(user, "other"))[1] == 0
    finally:
        if redis is not None:
            await redis.delete(*repo._keys(user, session))
            await redis.aclose()
