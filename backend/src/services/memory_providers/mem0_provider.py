from __future__ import annotations

from typing import Any

from src.core.config import settings
from src.core.logging import get_logger
from src.services.memory_providers.base import (
    MemoryHealth,
    MemoryProvider,
    MemorySnippet,
    MemoryWriteResult,
)


class Mem0Provider(MemoryProvider):
    # 目的：封裝 Mem0 SDK 的讀寫行為，對外提供統一記憶介面。
    # 為什麼：將第三方依賴隔離在 provider 層，便於降級與替換。

    def __init__(self, mode: str) -> None:
        self._log = get_logger('services.memory.mem0_provider')
        self._mode = str(mode or '').strip().lower()
        self._client = None
        self._init_error: str | None = None
        self._initialize_client()

    @property
    def name(self) -> str:
        return self._mode

    def _initialize_client(self) -> None:
        try:
            if self._mode == 'mem0_platform':
                from mem0 import MemoryClient

                api_key = str(settings.MEM0_API_KEY or '').strip()
                self._client = MemoryClient(api_key=api_key) if api_key else MemoryClient()
                return

            if self._mode == 'mem0_oss':
                from mem0 import Memory

                cfg = {
                    'vector_store': {
                        'provider': str(settings.MEM0_VECTOR_PROVIDER or 'qdrant').strip(),
                        'config': {
                            'host': str(settings.MEM0_QDRANT_HOST or 'localhost').strip(),
                            'port': int(settings.MEM0_QDRANT_PORT or 6333),
                            'collection_name': str(settings.MEM0_QDRANT_COLLECTION or 'dyagent_long_term_memories').strip(),
                        },
                    },
                    'llm': {
                        'provider': str(settings.MEM0_LLM_PROVIDER or 'openai').strip(),
                        'config': {
                            'api_base': str(settings.MEM0_LLM_API_BASE or '').strip(),
                            'model': str(settings.MEM0_LLM_MODEL or '').strip(),
                            'api_key': str(settings.MEM0_LLM_API_KEY or '').strip(),
                        },
                    },
                    'embedder': {
                        'provider': str(settings.MEM0_EMBEDDER_PROVIDER or 'openai').strip(),
                        'config': {
                            'api_base': str(settings.MEM0_EMBEDDER_API_BASE or '').strip(),
                            'model': str(settings.MEM0_EMBEDDER_MODEL or '').strip(),
                            'api_key': str(settings.MEM0_EMBEDDER_API_KEY or '').strip(),
                        },
                    },
                }
                self._client = Memory.from_config(cfg)
                return

            self._init_error = f'unsupported_mem0_mode:{self._mode}'
        except Exception as error:
            self._init_error = str(error)
            self._log.warning('memory.mem0.initialize_failed', mode=self._mode, error=self._init_error)

    def health(self) -> MemoryHealth:
        if self._client is None:
            return MemoryHealth(ok=False, provider=self.name, degraded=True, error=self._init_error or 'mem0_not_ready')
        return MemoryHealth(ok=True, provider=self.name)

    def search(
        self,
        *,
        query: str,
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
        app_id: str,
        top_k: int,
    ) -> list[MemorySnippet]:
        if self._client is None:
            return []
        try:
            filters: dict[str, Any] = {'app_id': app_id}
            if user_id:
                filters['user_id'] = user_id
            if agent_id:
                filters['agent_id'] = agent_id
            if run_id:
                filters['run_id'] = run_id

            raw = self._client.search(str(query or ''), filters=filters, top_k=max(1, int(top_k or 1)))
            rows = raw.get('results') if isinstance(raw, dict) else raw
            snippets: list[MemorySnippet] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                snippets.append(
                    MemorySnippet(
                        text=str(row.get('memory') or row.get('text') or '').strip(),
                        score=float(row.get('score') or 0.0),
                        scope_type=str((row.get('metadata') or {}).get('scope_type') or ''),
                        source='mem0',
                        metadata=dict(row.get('metadata') or {}),
                    )
                )
            return [s for s in snippets if s.text]
        except Exception as error:
            self._log.warning('memory.mem0.search_failed', error=str(error))
            return []

    def add(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
        app_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryWriteResult:
        if self._client is None:
            return MemoryWriteResult(ok=False, error='mem0_not_ready')
        try:
            kwargs: dict[str, Any] = {'app_id': app_id, 'metadata': dict(metadata or {})}
            if user_id:
                kwargs['user_id'] = user_id
            if agent_id:
                kwargs['agent_id'] = agent_id
            if run_id:
                kwargs['run_id'] = run_id
            self._client.add(messages, **kwargs)
            return MemoryWriteResult(ok=True)
        except Exception as error:
            self._log.warning('memory.mem0.add_failed', error=str(error))
            return MemoryWriteResult(ok=False, error=str(error))

    def forget_user(self, *, user_id: str, app_id: str) -> MemoryWriteResult:
        if self._client is None:
            return MemoryWriteResult(ok=False, error='mem0_not_ready')
        try:
            self._client.delete_all(user_id=str(user_id), app_id=str(app_id), confirm=True)
            return MemoryWriteResult(ok=True)
        except Exception as error:
            self._log.warning('memory.mem0.forget_user_failed', user_id=str(user_id), error=str(error))
            return MemoryWriteResult(ok=False, error=str(error))
