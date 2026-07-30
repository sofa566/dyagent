import json
from typing import Any

import redis.asyncio as redis

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
        await self.set(key, json.dumps(value), expire)

    def _build_short_term_memory_key(self, *, conversation_id: str) -> str:
        """目的：統一短期記憶 Redis key 命名。
        為什麼：避免 key 前綴散落在聊天流程，降低維護與清理成本。
        """
        return f'chat:short_term_memory:{str(conversation_id).strip()}'

    async def append_short_term_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        ttl_seconds: int | None = None,
        max_messages: int | None = None,
    ) -> None:
        # 目的：將最新對話訊息寫入短期記憶，並維持固定長度與 TTL。
        # 為什麼：聊天流程需要快速取得最近 N 則上下文，不應每次都查詢資料庫。
        if not self.client:
            return
        normalized_content = str(content or '').strip()
        if not normalized_content:
            return

        resolved_ttl_seconds = max(1, int(ttl_seconds or settings.SHORT_TERM_MEMORY_TTL_SEC or 3600))
        resolved_max_messages = max(1, int(max_messages or settings.SHORT_TERM_MEMORY_MAX_MESSAGES or 10))
        key = self._build_short_term_memory_key(conversation_id=conversation_id)
        payload = {
            'role': str(role or '').strip(),
            'content': normalized_content,
        }
        await self.client.lpush(key, json.dumps(payload, ensure_ascii=False))
        await self.client.ltrim(key, 0, resolved_max_messages - 1)
        await self.client.expire(key, resolved_ttl_seconds)

    async def get_short_term_messages(self, *, conversation_id: str, limit: int | None = None) -> list[dict[str, str]]:
        # 目的：取得短期記憶中的最近訊息（由舊到新）。
        # 為什麼：prompt 組裝需維持時間序，避免模型讀到倒序上下文。
        if not self.client:
            return []
        resolved_limit = max(1, int(limit or settings.SHORT_TERM_MEMORY_MAX_MESSAGES or 10))
        key = self._build_short_term_memory_key(conversation_id=conversation_id)
        rows = await self.client.lrange(key, 0, resolved_limit - 1)
        messages: list[dict[str, str]] = []
        for row in rows:
            try:
                parsed = json.loads(str(row or ''))
            except Exception:
                continue
            if not isinstance(parsed, dict):
                continue
            role = str(parsed.get('role') or '').strip()
            content = str(parsed.get('content') or '').strip()
            if not content or role not in {'user', 'assistant'}:
                continue
            messages.append({'role': role, 'content': content})
        messages.reverse()
        return messages


redis_service = RedisService()
