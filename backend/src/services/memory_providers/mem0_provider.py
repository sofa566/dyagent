from __future__ import annotations

import inspect
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


class Mem0Provider(MemoryProvider):
    # 目的：封裝 Mem0 SDK 的讀寫行為，對外提供統一記憶介面。
    # 為什麼：將第三方依賴隔離在 provider 層，便於降級與替換。

    def __init__(self, mode: str) -> None:
        self._log = get_logger('services.memory.mem0_provider')
        self._mode = str(mode or '').strip().lower()
        self._client = None
        self._init_error: str | None = None
        self._init_error_code: str | None = None
        self._last_error: str | None = None
        self._last_error_code: str | None = None
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

                self._patch_qdrant_search_compatibility()

                llm_config: dict[str, Any] = {
                    'model': str(settings.MEM0_LLM_MODEL or '').strip(),
                    'api_key': str(settings.MEM0_LLM_API_KEY or '').strip(),
                }
                llm_api_base = str(settings.MEM0_LLM_API_BASE or '').strip()
                if llm_api_base:
                    llm_config['base_url'] = llm_api_base

                embedder_config: dict[str, Any] = {
                    'model': str(settings.MEM0_EMBEDDER_MODEL or '').strip(),
                    'api_key': str(settings.MEM0_EMBEDDER_API_KEY or '').strip(),
                }
                embedder_api_base = str(settings.MEM0_EMBEDDER_API_BASE or '').strip()
                if embedder_api_base:
                    embedder_config['base_url'] = embedder_api_base

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
                        'config': llm_config,
                    },
                    'embedder': {
                        'provider': str(settings.MEM0_EMBEDDER_PROVIDER or 'openai').strip(),
                        'config': embedder_config,
                    },
                }
                self._client = Memory.from_config(cfg)
                return

            self._init_error = f'unsupported_mem0_mode:{self._mode}'
            self._init_error_code = 'provider_unavailable'
        except Exception as error:
            self._init_error = str(error)
            self._init_error_code = self._classify_error_code(error)
            self._log.warning('memory.mem0.initialize_failed', mode=self._mode, error=self._init_error)

    def _patch_qdrant_search_compatibility(self) -> None:
        # 目的：為新舊 qdrant-client 提供相容的 search 入口。
        # 為什麼：mem0ai 0.1.x 仍可能呼叫 QdrantClient.search，但新版 qdrant-client 只提供 query_points。
        try:
            from qdrant_client import QdrantClient
        except Exception:
            return

        if hasattr(QdrantClient, 'search'):
            return
        if not hasattr(QdrantClient, 'query_points'):
            return

        def _search_compat(self, collection_name: str, query_vector=None, query_filter=None, limit: int = 10, **kwargs):
            query = kwargs.pop('query', None)
            if query is None:
                query = query_vector

            if query_filter is None:
                query_filter = kwargs.pop('filter', None)

            response = self.query_points(
                collection_name=collection_name,
                query=query,
                query_filter=query_filter,
                limit=limit,
                **kwargs,
            )
            points = getattr(response, 'points', None)
            if points is not None:
                return points
            if isinstance(response, dict):
                return response.get('points', [])
            return response

        setattr(QdrantClient, 'search', _search_compat)
        self._log.warning('memory.mem0.qdrant_search_compat_enabled')

    def _classify_error_code(self, error: Exception) -> str:
        # 目的：將第三方 provider 例外歸類為可觀測錯誤碼。
        # 為什麼：治理層需區分 timeout/auth/provider_unavailable，便於告警與排障。
        message = str(error or '').strip().lower()
        timeout_tokens = ('timeout', 'timed out', 'deadline exceeded', 'read timeout', 'connect timeout')
        auth_tokens = ('unauthorized', 'forbidden', 'invalid api key', 'api key', 'authentication', '401', '403')
        if any(token in message for token in timeout_tokens):
            return 'timeout'
        if any(token in message for token in auth_tokens):
            return 'auth'
        return 'provider_unavailable'

    def _remember_last_error(self, *, error: Exception) -> None:
        self._last_error = str(error)
        self._last_error_code = self._classify_error_code(error)

    def _clear_last_error(self) -> None:
        self._last_error = None
        self._last_error_code = None

    def _resolve_scope_type(
        self,
        *,
        metadata: dict[str, Any] | None,
    ) -> str:
        # 目的：正規化 scope_type 並提供預設值。
        # 為什麼：不同呼叫端可能缺少 scope 設定，需確保資料分群一致。
        allowed = {'user_scope', 'agent_scope', 'interaction_scope', 'global_scope'}
        scope_type = str((metadata or {}).get('scope_type') or '').strip().lower()
        if scope_type in allowed:
            return scope_type
        return 'interaction_scope'

    def _build_scoped_lookup_filters(
        self,
        *,
        app_id: str,
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        # 目的：建立檢索過濾條件。
        # 為什麼：部分 mem0 版本不會保存 app_id 維度，若強制帶 app_id filter 會查不到已寫入記憶。
        filters: dict[str, Any] = {}
        return filters

    def _is_visible_by_scope(
        self,
        *,
        scope_type: str,
        metadata: dict[str, Any],
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
    ) -> bool:
        # 目的：依 scope 規則判斷記憶片段是否對當前請求可見。
        # 為什麼：實作 user/agent/interaction/global 隔離，避免跨範圍資料誤用。
        meta_user_id = str(metadata.get('user_id') or '').strip()
        meta_agent_id = str(metadata.get('agent_id') or '').strip()
        meta_run_id = str(metadata.get('run_id') or '').strip()

        if scope_type == 'global_scope':
            return True
        if scope_type == 'user_scope':
            return bool(user_id) and (meta_user_id == str(user_id))
        if scope_type == 'agent_scope':
            return bool(agent_id) and (meta_agent_id == str(agent_id))
        if scope_type == 'interaction_scope':
            if not user_id:
                return False
            if meta_user_id != str(user_id):
                return False

            if agent_id:
                if meta_agent_id != str(agent_id):
                    return False
                if meta_run_id and run_id:
                    return meta_run_id == str(run_id)
            return True
        return False

    def _build_scoped_write_kwargs(
        self,
        *,
        app_id: str,
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # 目的：依 scope_type 產生寫入參數與 metadata。
        # 為什麼：確保不同記憶範圍可被一致檢索，並避免跨作用域污染。
        scope_type = self._resolve_scope_type(metadata=metadata)
        scoped_metadata = dict(metadata or {})
        scoped_metadata['scope_type'] = scope_type
        scoped_metadata['app_id'] = app_id

        kwargs: dict[str, Any] = {'app_id': app_id, 'metadata': scoped_metadata}
        if scope_type == 'global_scope':
            return kwargs
        if scope_type == 'user_scope':
            if user_id:
                kwargs['user_id'] = user_id
                scoped_metadata['user_id'] = user_id
            return kwargs
        if scope_type == 'agent_scope':
            if agent_id:
                kwargs['agent_id'] = agent_id
                scoped_metadata['agent_id'] = agent_id
            return kwargs

        if user_id:
            kwargs['user_id'] = user_id
            scoped_metadata['user_id'] = user_id
        if agent_id:
            kwargs['agent_id'] = agent_id
            scoped_metadata['agent_id'] = agent_id
        if run_id:
            kwargs['run_id'] = run_id
            scoped_metadata['run_id'] = run_id
        return kwargs

    def health(self) -> MemoryHealth:
        if self._client is None:
            return MemoryHealth(
                ok=False,
                provider=self.name,
                degraded=True,
                error=self._init_error or 'mem0_not_ready',
                error_code=self._init_error_code or 'provider_unavailable',
            )
        if self._last_error:
            return MemoryHealth(
                ok=True,
                provider=self.name,
                degraded=True,
                error=self._last_error,
                error_code=self._last_error_code,
            )
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
            raise MemoryProviderError(code='provider_unavailable', message='mem0_not_ready')
        try:
            filters = self._build_scoped_lookup_filters(
                app_id=app_id,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
            )

            search_kwargs = self._build_search_kwargs(
                app_id=app_id,
                filters=filters,
                top_k=max(1, int(top_k or 1)),
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
            )
            raw = self._client.search(str(query or ''), **search_kwargs)
            rows = raw.get('results') if isinstance(raw, dict) else raw

            if (not rows) and filters:
                relaxed_kwargs = self._build_search_kwargs(
                    app_id=app_id,
                    filters={},
                    top_k=max(1, int(top_k or 1)),
                    user_id=user_id,
                    agent_id=agent_id,
                    run_id=run_id,
                )
                raw = self._client.search(str(query or ''), **relaxed_kwargs)
            rows = raw.get('results') if isinstance(raw, dict) else raw
            snippets: list[MemorySnippet] = []
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                metadata = dict(row.get('metadata') or {})
                if 'user_id' not in metadata and row.get('user_id'):
                    metadata['user_id'] = str(row.get('user_id'))
                if 'agent_id' not in metadata and row.get('agent_id'):
                    metadata['agent_id'] = str(row.get('agent_id'))
                if 'run_id' not in metadata and row.get('run_id'):
                    metadata['run_id'] = str(row.get('run_id'))
                if 'app_id' not in metadata and row.get('app_id'):
                    metadata['app_id'] = str(row.get('app_id'))
                scope_type = str(metadata.get('scope_type') or '').strip().lower() or 'interaction_scope'
                if not self._is_visible_by_scope(
                    scope_type=scope_type,
                    metadata=metadata,
                    user_id=user_id,
                    agent_id=agent_id,
                    run_id=run_id,
                ):
                    continue
                snippets.append(
                    MemorySnippet(
                        text=str(row.get('memory') or row.get('text') or '').strip(),
                        score=float(row.get('score') or 0.0),
                        scope_type=scope_type,
                        source='mem0',
                        metadata=metadata,
                    )
                )
            self._clear_last_error()
            return [s for s in snippets if s.text]
        except Exception as error:
            self._remember_last_error(error=error)
            self._log.warning('memory.mem0.search_failed', error=str(error), error_code=self._last_error_code)
            raise MemoryProviderError(code=str(self._last_error_code or 'provider_unavailable'), message=str(error)) from error

    def _build_search_kwargs(
        self,
        *,
        app_id: str,
        filters: dict[str, Any],
        top_k: int,
        user_id: str | None,
        agent_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any]:
        # 目的：依目前 mem0 client 版本動態組裝 search 參數。
        # 為什麼：不同版本的 Memory.search 參數命名差異（top_k/limit、filters/filter），需相容處理。
        kwargs: dict[str, Any] = {}
        try:
            signature = inspect.signature(self._client.search)
            parameters = signature.parameters
            param_names = set(parameters.keys())
            accepts_var_keyword = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except Exception:
            param_names = set()
            accepts_var_keyword = True

        def can_pass(name: str) -> bool:
            return accepts_var_keyword or (name in param_names)

        if can_pass('top_k'):
            kwargs['top_k'] = top_k
        elif can_pass('limit'):
            kwargs['limit'] = top_k

        if can_pass('filters'):
            kwargs['filters'] = filters
        elif can_pass('filter'):
            kwargs['filter'] = filters

        if can_pass('app_id'):
            kwargs['app_id'] = app_id
        if user_id and can_pass('user_id'):
            kwargs['user_id'] = user_id
        if agent_id and can_pass('agent_id'):
            kwargs['agent_id'] = agent_id
        if run_id and can_pass('run_id'):
            kwargs['run_id'] = run_id

        return kwargs

    def _build_add_kwargs(self, *, raw_kwargs: dict[str, Any]) -> dict[str, Any]:
        # 目的：依目前 mem0 client 版本動態組裝 add 參數。
        # 為什麼：不同版本的 Memory.add 可接受參數不同，需避免傳入不支援欄位導致寫入失敗。
        try:
            signature = inspect.signature(self._client.add)
            parameters = signature.parameters
            param_names = set(parameters.keys())
            accepts_var_keyword = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except Exception:
            param_names = set()
            accepts_var_keyword = True

        def can_pass(name: str) -> bool:
            return accepts_var_keyword or (name in param_names)

        kwargs: dict[str, Any] = {}
        for key, value in raw_kwargs.items():
            if key == 'app_id':
                continue
            if value is None:
                continue
            if can_pass(key):
                kwargs[key] = value
        return kwargs

    def _build_forget_kwargs(self, *, user_id: str, app_id: str) -> dict[str, Any]:
        # 目的：依目前 mem0 client 版本動態組裝 delete_all 參數。
        # 為什麼：不同版本對 app_id/confirm 支援不同，需避免治理 API 在刪除流程因參數不相容而失敗。
        base_kwargs: dict[str, Any] = {'user_id': str(user_id), 'app_id': str(app_id), 'confirm': True}
        try:
            signature = inspect.signature(self._client.delete_all)
            parameters = signature.parameters
            param_names = set(parameters.keys())
            accepts_var_keyword = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except Exception:
            param_names = set()
            accepts_var_keyword = True

        def can_pass(name: str) -> bool:
            return accepts_var_keyword or (name in param_names)

        kwargs: dict[str, Any] = {}
        for key, value in base_kwargs.items():
            if can_pass(key):
                kwargs[key] = value
        return kwargs

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
            return MemoryWriteResult(ok=False, error='mem0_not_ready', error_code='provider_unavailable')
        try:
            scoped_kwargs = self._build_scoped_write_kwargs(
                app_id=app_id,
                user_id=user_id,
                agent_id=agent_id,
                run_id=run_id,
                metadata=metadata,
            )
            kwargs = self._build_add_kwargs(raw_kwargs=scoped_kwargs)
            self._client.add(messages, **kwargs)
            self._clear_last_error()
            return MemoryWriteResult(ok=True)
        except Exception as error:
            self._remember_last_error(error=error)
            self._log.warning('memory.mem0.add_failed', error=str(error), error_code=self._last_error_code)
            return MemoryWriteResult(ok=False, error=str(error), error_code=self._last_error_code)

    def forget_user(self, *, user_id: str, app_id: str) -> MemoryWriteResult:
        if self._client is None:
            return MemoryWriteResult(ok=False, error='mem0_not_ready', error_code='provider_unavailable')
        try:
            kwargs = self._build_forget_kwargs(user_id=user_id, app_id=app_id)
            self._client.delete_all(**kwargs)
            self._clear_last_error()
            return MemoryWriteResult(ok=True)
        except Exception as error:
            self._remember_last_error(error=error)
            self._log.warning(
                'memory.mem0.forget_user_failed',
                user_id=str(user_id),
                error=str(error),
                error_code=self._last_error_code,
            )
            return MemoryWriteResult(ok=False, error=str(error), error_code=self._last_error_code)
