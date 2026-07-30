from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemorySnippet:
    text: str
    score: float = 0.0
    scope_type: str = ''
    source: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryHealth:
    ok: bool
    provider: str
    degraded: bool = False
    error: str | None = None
    error_code: str | None = None


@dataclass
class MemoryWriteResult:
    ok: bool
    error: str | None = None
    error_code: str | None = None


class MemoryProviderError(Exception):
    # 目的：提供 provider 層可辨識的錯誤型別。
    # 為什麼：memory service 需依錯誤分類（timeout/auth/provider_unavailable）做 fail-open 與觀測。

    def __init__(self, *, code: str, message: str):
        super().__init__(message)
        self.code = str(code or 'provider_unavailable')


class MemoryProvider(ABC):
    # 目的：定義記憶供應器統一介面，避免路由層直接耦合第三方 SDK。
    # 為什麼：支援 off/mock/mem0_oss/mem0_platform 切換，並可在故障時降級。

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> MemoryHealth:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def forget_user(self, *, user_id: str, app_id: str) -> MemoryWriteResult:
        raise NotImplementedError
