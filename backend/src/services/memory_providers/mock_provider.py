from __future__ import annotations

from typing import Any

from src.services.memory_providers.base import (
    MemoryHealth,
    MemoryProvider,
    MemorySnippet,
    MemoryWriteResult,
)


class MockMemoryProvider(MemoryProvider):
    # 目的：提供可預期的記憶供應器假實作，供開發與測試使用。
    # 為什麼：測試不應依賴外部 Mem0/Qdrant 網路狀態，避免 flaky case。

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return 'mock'

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
        normalized_query = str(query or '').strip().lower()
        matches: list[MemorySnippet] = []
        for row in self._records:
            if row.get('app_id') != app_id:
                continue
            if user_id is not None and row.get('user_id') != user_id:
                continue
            if agent_id is not None and row.get('agent_id') != agent_id:
                continue
            content = str(row.get('text') or '')
            if normalized_query and normalized_query not in content.lower():
                continue
            matches.append(
                MemorySnippet(
                    text=content,
                    score=1.0,
                    scope_type=str(row.get('scope_type') or ''),
                    source=self.name,
                    metadata=dict(row.get('metadata') or {}),
                )
            )
            if len(matches) >= max(1, int(top_k or 1)):
                break
        return matches

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
        text_parts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get('role') or '').strip()
            content = str(msg.get('content') or '').strip()
            if not content:
                continue
            text_parts.append(f"{role}: {content}" if role else content)
        if not text_parts:
            return MemoryWriteResult(ok=False, error='empty_messages')

        record = {
            'app_id': app_id,
            'user_id': user_id,
            'agent_id': agent_id,
            'run_id': run_id,
            'text': '\n'.join(text_parts),
            'scope_type': str((metadata or {}).get('scope_type') or ''),
            'metadata': dict(metadata or {}),
        }
        self._records.append(record)
        return MemoryWriteResult(ok=True)

    def forget_user(self, *, user_id: str, app_id: str) -> MemoryWriteResult:
        remained = []
        for row in self._records:
            if row.get('app_id') == app_id and row.get('user_id') == user_id:
                continue
            remained.append(row)
        self._records = remained
        return MemoryWriteResult(ok=True)
