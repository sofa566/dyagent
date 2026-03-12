import redis.asyncio as redis
from typing import Any

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger(__name__)


class RedisService:
    def __init__(self):
        self.client: redis.Redis | None = None

    async def connect(self):
        try:
            self.client = redis.from_url(
                settings.REDIS_URL,
                encoding='utf-8',
                decode_responses=True,
            )
            await self.client.ping()
            logger.info('redis_connected', url=settings.REDIS_URL)
        except Exception as e:
            logger.error('redis_connection_failed', error=str(e))
            self.client = None

    async def disconnect(self):
        if self.client:
            await self.client.close()
            logger.info('redis_disconnected')

    async def get(self, key: str) -> str | None:
        if not self.client:
            return None
        return await self.client.get(key)

    async def set(self, key: str, value: str, expire: int | None = None):
        if not self.client:
            return
        await self.client.set(key, value, ex=expire)

    async def delete(self, key: str):
        if not self.client:
            return
        await self.client.delete(key)

    async def get_json(self, key: str) -> Any:
        value = await self.get(key)
        if value:
            import json
            return json.loads(value)
        return None

    async def set_json(self, key: str, value: Any, expire: int | None = None):
        import json
        await self.set(key, json.dumps(value), expire)


redis_service = RedisService()
