from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from src.core.config import settings
from src.core.logging import get_logger
from src.services.memory_providers.base import (
    MemoryHealth,
    MemoryProvider,
    MemoryProviderError,
    MemorySnippet,
    MemoryWriteResult,
)
from src.services.memory_providers.mem0_provider import Mem0Provider
from src.services.memory_providers.mock_provider import MockMemoryProvider


class OffMemoryProvider(MemoryProvider):
    # 目的：提供關閉記憶模式的 no-op 行為。
    # 為什麼：在不啟用記憶時維持同一呼叫介面，避免路由層分支散落。

    @property
    def name(self) -> str:
        return 'off'

    def health(self) -> MemoryHealth:
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
        return MemoryWriteResult(ok=True)

    def forget_user(self, *, user_id: str, app_id: str) -> MemoryWriteResult:
        return MemoryWriteResult(ok=True)


@dataclass
class MemoryRetrieveResult:
    snippets: list[MemorySnippet]
    elapsed_ms: int
    provider: str
    ok: bool
    error: str | None = None
    error_code: str | None = None


class MemoryService:
    # 目的：集中記憶讀寫策略與 provider 切換，作為聊天流程唯一入口。
    # 為什麼：避免業務層直接依賴第三方 SDK，並可統一實作 fail-open 降級。

    def __init__(self, provider: MemoryProvider | None = None) -> None:
        self._log = get_logger('services.memory')
        self._provider = provider or self._build_provider()

    def _build_provider(self) -> MemoryProvider:
        mode = str(settings.AGENT_MEMORY_PROVIDER or 'off').strip().lower()
        if mode == 'mock':
            return MockMemoryProvider()
        if mode in {'mem0_oss', 'mem0_platform'}:
            return Mem0Provider(mode)
        return OffMemoryProvider()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def health(self) -> MemoryHealth:
        return self._provider.health()

    def retrieve(
        self,
        *,
        query: str,
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
        app_id: str | None = None,
        top_k: int | None = None,
    ) -> MemoryRetrieveResult:
        started_at = time.monotonic()
        if not bool(getattr(settings, 'AGENT_MEMORY_READ_ENABLED', True)):
            return MemoryRetrieveResult(snippets=[], elapsed_ms=0, provider=self.provider_name, ok=True)

        resolved_app_id = str(app_id or settings.AGENT_MEMORY_APP_ID or 'dyagent').strip()
        resolved_top_k = max(1, int(top_k or settings.AGENT_MEMORY_TOP_K or 5))
        try:
            snippets = self._provider.search(
                query=query,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                app_id=resolved_app_id,
                top_k=resolved_top_k,
            )
            return MemoryRetrieveResult(
                snippets=snippets,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                provider=self.provider_name,
                ok=True,
            )
        except Exception as error:
            error_code = None
            if isinstance(error, MemoryProviderError):
                error_code = error.code
            self._log.warning('memory.retrieve_failed', error=str(error))
            return MemoryRetrieveResult(
                snippets=[],
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                provider=self.provider_name,
                ok=False,
                error=str(error),
                error_code=error_code,
            )

    def write(
        self,
        *,
        messages: list[dict[str, str]],
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
        metadata: dict[str, Any] | None = None,
        app_id: str | None = None,
    ) -> MemoryWriteResult:
        if not bool(getattr(settings, 'AGENT_MEMORY_WRITE_ENABLED', True)):
            return MemoryWriteResult(ok=True)

        resolved_app_id = str(app_id or settings.AGENT_MEMORY_APP_ID or 'dyagent').strip()
        try:
            return self._provider.add(
                messages=messages,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                app_id=resolved_app_id,
                metadata=metadata,
            )
        except Exception as error:
            self._log.warning('memory.write_failed', error=str(error))
            return MemoryWriteResult(ok=False, error=str(error))

    def forget_user(self, *, user_id: str, app_id: str | None = None) -> MemoryWriteResult:
        resolved_app_id = str(app_id or settings.AGENT_MEMORY_APP_ID or 'dyagent').strip()
        try:
            return self._provider.forget_user(user_id=str(user_id), app_id=resolved_app_id)
        except Exception as error:
            self._log.warning('memory.forget_user_failed', user_id=str(user_id), error=str(error))
            return MemoryWriteResult(ok=False, error=str(error))


memory_service = MemoryService()
