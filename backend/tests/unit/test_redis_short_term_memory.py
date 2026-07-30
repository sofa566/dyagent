from __future__ import annotations

from src.services.redis_service import RedisService


class _FakeRedisClient:
    def __init__(self) -> None:
        self._store: dict[str, list[str]] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def lpush(self, key: str, value: str) -> None:
        rows = self._store.get(key, [])
        rows.insert(0, value)
        self._store[key] = rows

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        rows = self._store.get(key, [])
        self._store[key] = rows[start:stop + 1]

    async def expire(self, key: str, ttl: int) -> None:
        self.expire_calls.append((key, ttl))

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        rows = self._store.get(key, [])
        return rows[start:stop + 1]


async def test_append_short_term_message_trims_and_sets_ttl() -> None:
    service = RedisService()
    fake_client = _FakeRedisClient()
    service.client = fake_client

    await service.append_short_term_message(
        conversation_id='conv-a',
        role='user',
        content='第一則',
        ttl_seconds=60,
        max_messages=2,
    )
    await service.append_short_term_message(
        conversation_id='conv-a',
        role='assistant',
        content='第二則',
        ttl_seconds=60,
        max_messages=2,
    )
    await service.append_short_term_message(
        conversation_id='conv-a',
        role='user',
        content='第三則',
        ttl_seconds=60,
        max_messages=2,
    )

    rows = await service.get_short_term_messages(conversation_id='conv-a', limit=10)
    assert len(rows) == 2
    assert rows[0]['content'] == '第二則'
    assert rows[1]['content'] == '第三則'
    assert fake_client.expire_calls


async def test_get_short_term_messages_returns_old_to_new_order() -> None:
    service = RedisService()
    fake_client = _FakeRedisClient()
    service.client = fake_client

    await service.append_short_term_message(
        conversation_id='conv-b',
        role='user',
        content='使用者提問',
        ttl_seconds=120,
        max_messages=5,
    )
    await service.append_short_term_message(
        conversation_id='conv-b',
        role='assistant',
        content='助理回答',
        ttl_seconds=120,
        max_messages=5,
    )

    rows = await service.get_short_term_messages(conversation_id='conv-b', limit=5)
    assert [item['role'] for item in rows] == ['user', 'assistant']
    assert [item['content'] for item in rows] == ['使用者提問', '助理回答']
