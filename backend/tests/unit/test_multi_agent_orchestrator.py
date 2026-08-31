"""多代理協作 Orchestrator 單元與整合測試（T070–T074）

測試策略：
- T070 直接匯入並呼叫純函式，不需 DB/LLM
- T071–T074 透過 FastAPI TestClient 呼叫 POST /api/chat，
  以 mock 隔離 LLM 與子代理串流，專注驗證協調邏輯
"""
from __future__ import annotations

import json
import types
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.api.routes.chat import (
    _build_execution_waves,
    _classify_routing,
    _inject_prior_context,
    _pick_worker_agent,
)
from src.core.config import settings
from src.models import Agent, MultiAgentSession, MultiAgentTask, Workspace


# ──────────────────────────────────────────────
# 輔助：產生測試用 stub（不走 SQLAlchemy ORM）
# ──────────────────────────────────────────────

def _make_agent(name: str, description: str = '', is_router: bool = False) -> types.SimpleNamespace:
    """回傳 SimpleNamespace，行為等同 Agent 但無 ORM 開銷。"""
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        description=description,
        is_router=is_router,
        model_type='cloud',
        model_config={},
    )


def _make_task(index: int, depends_on: list[int] | None = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        task_index=index,
        depends_on=depends_on or [],
        status='pending',
        result_text=None,
        error=None,
    )


# ──────────────────────────────────────────────
# T070：_classify_routing 各層判斷
# ──────────────────────────────────────────────

class TestClassifyRouting:
    """Given/When/Then 結構，驗證三層快路徑判斷邏輯。"""

    def _workers(self, n: int = 2) -> list[Agent]:
        return [_make_agent(f'Worker{i}', f'Worker {i} description') for i in range(n)]

    # ── 第一層：快速排除 ──

    def test_short_message_returns_single(self):
        """Given 訊息 < 15 字元，When 呼叫 _classify_routing，Then 回傳 single。"""
        result = _classify_routing(message='你好', workers=self._workers())
        assert result == 'single'

    def test_fewer_than_two_workers_returns_single(self):
        """Given workers < 2，When 呼叫 _classify_routing，Then 回傳 single。"""
        result = _classify_routing(
            message='請查詢銷售報表並且同時發通知給主管',
            workers=self._workers(1),
        )
        assert result == 'single'

    def test_no_compound_signal_returns_single(self):
        """Given 無複合意圖信號的長訊息，When 呼叫 _classify_routing，Then 回傳 single。"""
        result = _classify_routing(
            message='請告訴我今天的天氣預報狀況如何謝謝你',
            workers=self._workers(),
        )
        assert result == 'single'

    # ── 第二層 + 第三層：有複合信號時 ──

    def test_compound_signal_with_no_embedding_returns_multi(self):
        """Given 含複合信號且 embedding 不可用，When 呼叫 _classify_routing，
        Then 跳過 embedding 層並回傳 multi。"""
        with patch('src.api.routes.chat.embedding_service') as mock_emb:
            mock_emb.embed_one.side_effect = Exception('no embedding')
            result = _classify_routing(
                message='請查詢銷售報表並且同時發通知給主管',
                workers=self._workers(),
            )
        assert result == 'multi'

    def test_mention_trigger_returns_multi_without_embedding(self):
        """Given 訊息含 @代理者名稱，When 呼叫 _classify_routing，Then 走多代理路徑。"""
        workers = [_make_agent('銷售代理'), _make_agent('通知代理')]
        with patch('src.api.routes.chat.embedding_service') as mock_emb:
            mock_emb.embed_one.side_effect = Exception('no embedding')
            result = _classify_routing(
                message='請 @銷售代理 查詢報表，然後 @通知代理 發送結果給主管',
                workers=workers,
            )
        assert result == 'multi'

    def test_embedding_large_gap_returns_single(self):
        """Given embedding 前兩名差距 > 0.2，When 呼叫 _classify_routing，
        Then 判斷為單代理（最高分 Worker 明顯更合適）。"""
        def fake_embed(text: str):
            # Worker0 描述 → 高分向量；Worker1 → 低分向量
            if 'Worker0' in text or 'worker0' in text.lower():
                return [1.0, 0.0]
            if 'Worker1' in text or 'worker1' in text.lower():
                return [0.0, 1.0]
            # 訊息向量：與 Worker0 完全吻合
            return [1.0, 0.0]

        workers = [
            _make_agent('Worker0', 'Worker0 description'),
            _make_agent('Worker1', 'Worker1 description'),
        ]
        with patch('src.api.routes.chat_tools.embedding_service') as mock_emb:
            mock_emb.embed_one.side_effect = fake_embed
            result = _classify_routing(
                message='請查詢銷售報表並且同時發通知給主管',
                workers=workers,
            )
        assert result == 'single'

    def test_embedding_small_gap_returns_multi(self):
        """Given embedding 前兩名差距 ≤ 0.1，When 呼叫 _classify_routing，
        Then 進入第三層並回傳 multi（embedding 無法明確判斷）。"""
        def fake_embed(_text: str):
            return [0.7, 0.7]  # 所有向量都一樣 → gap = 0

        with patch('src.api.routes.chat.embedding_service') as mock_emb:
            mock_emb.embed_one.side_effect = fake_embed
            result = _classify_routing(
                message='請查詢銷售報表並且同時發通知給主管',
                workers=self._workers(),
            )
        assert result == 'multi'


# ──────────────────────────────────────────────
# T070（補）：_build_execution_waves 拓撲排序
# ──────────────────────────────────────────────

class TestBuildExecutionWaves:
    def test_no_dependencies_all_in_one_wave(self):
        """Given 3 個無依賴任務，When 建立 waves，Then 只有 1 波包含全部任務。"""
        tasks = [_make_task(i) for i in range(3)]
        waves = _build_execution_waves(tasks)
        assert len(waves) == 1
        assert len(waves[0]) == 3

    def test_linear_dependencies_create_separate_waves(self):
        """Given task1 depends on task0，When 建立 waves，Then 產生 2 波（循序）。"""
        t0 = _make_task(0)
        t1 = _make_task(1, depends_on=[0])
        waves = _build_execution_waves([t0, t1])
        assert len(waves) == 2
        assert waves[0][0].task_index == 0
        assert waves[1][0].task_index == 1

    def test_diamond_dependency(self):
        """Given t2、t3 都依賴 t0，t4 依賴 t2 與 t3，When 建立 waves，Then 3 波。"""
        t0 = _make_task(0)
        t2 = _make_task(1, depends_on=[0])
        t3 = _make_task(2, depends_on=[0])
        t4 = _make_task(3, depends_on=[1, 2])
        waves = _build_execution_waves([t0, t2, t3, t4])
        assert len(waves) == 3
        assert len(waves[1]) == 2  # t2, t3 可並行

    def test_empty_tasks_returns_empty_waves(self):
        """Given 空任務清單，When 建立 waves，Then 回傳空清單。"""
        assert _build_execution_waves([]) == []


# ──────────────────────────────────────────────
# T070（補）：_inject_prior_context
# ──────────────────────────────────────────────

class TestInjectPriorContext:
    def test_no_prior_results_returns_original(self):
        """Given 無前置結果，When 注入，Then 回傳原始任務描述不變。"""
        result = _inject_prior_context(task_desc='查詢銷售報表', prior_results=[])
        assert result == '查詢銷售報表'

    def test_prior_result_prepended(self):
        """Given 一筆前置結果，When 注入，Then 回傳包含背景資訊前綴的字串。"""
        result = _inject_prior_context(
            task_desc='發送通知',
            prior_results=[{'agent_name': '銷售代理', 'result_text': '銷售額 100 萬'}],
        )
        assert '前置任務結果' in result
        assert '銷售代理' in result
        assert '銷售額 100 萬' in result
        assert '發送通知' in result

    def test_long_result_truncated_to_2000_chars(self):
        """Given result_text > 2000 字元，When 注入，Then 截斷到 2000 字元。"""
        long_text = 'B' * 3000
        result = _inject_prior_context(
            task_desc='後置任務',
            prior_results=[{'agent_name': 'AgentX', 'result_text': long_text}],
        )
        # 截斷後只有 2000 個 'B'
        assert result.count('B') == 2000


class TestWorkerClassRouting:
    """驗證主代理分派遵守 tasked/public 與 enabled 規則。"""

    def test_pick_tasked_when_tasked_matches(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        tasked = Agent(
            name='銷售任務代理', description='處理銷售查詢', model_type='cloud',
            agent_class='tasked', enabled=True, workspace_id=workspace.id,
        )
        public = Agent(
            name='公眾代理', description='一般問題回覆', model_type='cloud',
            agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(tasked); db.add(public)
        db.commit(); db.refresh(router); db.refresh(tasked); db.refresh(public)

        worker, reason = _pick_worker_agent(db=db, router_agent=router, message='請銷售任務代理處理本月銷售報表')
        assert worker is not None
        assert str(worker.id) == str(tasked.id)
        assert reason.startswith('tasked_')

    def test_fallback_to_public_when_no_tasked(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        public = Agent(
            name='公眾代理', description='一般問題回覆', model_type='cloud',
            agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(public)
        db.commit(); db.refresh(router); db.refresh(public)

        worker, reason = _pick_worker_agent(db=db, router_agent=router, message='今天天氣如何')
        assert worker is not None
        assert str(worker.id) == str(public.id)
        assert reason.startswith('public_')

    def test_disabled_agent_not_selected(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        tasked_disabled = Agent(
            name='停用任務代理', description='銷售查詢', model_type='cloud',
            agent_class='tasked', enabled=False, workspace_id=workspace.id,
        )
        public = Agent(
            name='公眾代理', description='一般問題回覆', model_type='cloud',
            agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(tasked_disabled); db.add(public)
        db.commit(); db.refresh(router); db.refresh(tasked_disabled); db.refresh(public)

        worker, reason = _pick_worker_agent(db=db, router_agent=router, message='請問銷售報表')
        assert worker is not None
        assert str(worker.id) == str(public.id)
        assert reason.startswith('public_') or reason == 'public_default_fallback'

    def test_private_agent_priority_when_user_has_entity_permission(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        private_worker = Agent(
            name='財務私有代理', description='處理財務內部任務', model_type='cloud',
            agent_class='private', enabled=True, workspace_id=workspace.id,
        )
        tasked = Agent(
            name='一般任務代理', description='處理一般任務', model_type='cloud',
            agent_class='tasked', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(private_worker); db.add(tasked)
        db.commit(); db.refresh(router); db.refresh(private_worker); db.refresh(tasked)

        private_permission_key = f'entity.agent.{str(private_worker.id)}.execute'
        worker, reason = _pick_worker_agent(
            db=db,
            router_agent=router,
            message='請財務私有代理協助整理月報',
            private_agent_ids={private_permission_key},
        )

        assert worker is not None
        assert str(worker.id) == str(private_worker.id)
        assert reason.startswith('private_')

    def test_private_agent_excluded_without_entity_permission(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        private_worker = Agent(
            name='法務私有代理', description='處理法務內部任務', model_type='cloud',
            agent_class='private', enabled=True, workspace_id=workspace.id,
        )
        public = Agent(
            name='公眾代理', description='一般問題回覆', model_type='cloud',
            agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(private_worker); db.add(public)
        db.commit(); db.refresh(router); db.refresh(private_worker); db.refresh(public)

        worker, reason = _pick_worker_agent(
            db=db,
            router_agent=router,
            message='請法務私有代理協助我',
            private_agent_ids=set(),
        )

        assert worker is not None
        assert str(worker.id) == str(public.id)
        assert reason.startswith('public_')

    def test_short_message_falls_back_without_llm_judge(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        public = Agent(
            name='公眾代理', description='一般問題回覆', model_type='cloud',
            agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(public)
        db.commit(); db.refresh(router); db.refresh(public)

        with patch('src.api.routes.chat._pick_worker_by_llm', side_effect=RuntimeError('should_not_call')):
            worker, reason = _pick_worker_agent(db=db, router_agent=router, message='你是誰？')

        assert worker is not None
        assert str(worker.id) == str(public.id)
        assert reason.startswith('public_')

    def test_router_assignment_mode_description_only_ignores_skill_hint(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        tasked = Agent(
            name='銷售任務代理', description='處理銷售查詢', model_type='cloud',
            agent_class='tasked', enabled=True, workspace_id=workspace.id,
        )
        public = Agent(
            name='公眾代理', description='一般問題回覆', model_type='cloud',
            agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(tasked); db.add(public)
        db.commit(); db.refresh(router); db.refresh(tasked); db.refresh(public)

        with (
            patch.object(settings, 'ROUTER_ASSIGNMENT_MODE', 'description_only'),
            patch('src.api.routes.chat._detect_intent_skill_name', return_value='fake-skill'),
        ):
            worker, reason = _pick_worker_agent(db=db, router_agent=router, message='請協助我處理銷售報表')

        assert worker is not None
        assert str(worker.id) in {str(tasked.id), str(public.id)}
        assert 'skill_hint_' not in reason

    def test_router_assignment_mode_memory_first_uses_memory_signal(self, db, workspace):
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        tasked = Agent(
            name='程式任務代理', description='程式優化', model_type='cloud',
            agent_class='tasked', enabled=True, workspace_id=workspace.id,
        )
        public = Agent(
            name='公眾代理', description='一般問題回覆', model_type='cloud',
            agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router); db.add(tasked); db.add(public)
        db.commit(); db.refresh(router); db.refresh(tasked); db.refresh(public)

        def _fake_memory_retrieve(*args, **kwargs):
            agent_id = str(kwargs.get('agent_id') or '')
            if agent_id == str(tasked.id):
                memory_snippet = types.SimpleNamespace(
                    text='過往偏好：程式需求優先交給程式任務代理',
                    score=0.95,
                    scope_type='interaction_scope',
                )
                return types.SimpleNamespace(ok=True, snippets=[memory_snippet], provider='mock')
            return types.SimpleNamespace(ok=True, snippets=[], provider='mock')

        with (
            patch.object(settings, 'ROUTER_ASSIGNMENT_MODE', 'memory_first'),
            patch.object(settings, 'AGENT_MEMORY_ROUTING_MODE', 'memory_first'),
            patch('src.api.routes.chat.memory_service.retrieve', side_effect=_fake_memory_retrieve),
        ):
            worker, reason = _pick_worker_agent(
                db=db,
                router_agent=router,
                message='請幫我優化這段 Python 程式',
                user_id='user-a',
            )

        assert worker is not None
        assert str(worker.id) == str(tasked.id)
        assert 'memory' in reason


# ──────────────────────────────────────────────
# T073：回歸測試 — 簡單訊息走原有單代理路徑
# ──────────────────────────────────────────────

class TestSingleAgentRegression:
    """確認多代理邏輯不破壞原有單代理 SSE 流程。"""

    def test_simple_message_no_orchestrator_events(self, client, admin_token, db, workspace):
        """Given 短訊息（< 15 字），When POST /api/chat，
        Then SSE 無 orchestrator.plan 事件，且有 route.decision 事件。"""
        # 建立 router agent
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, workspace_id=workspace.id,
        )
        db.add(router)
        worker = Agent(
            name='Worker', description='工作代理', model_type='cloud',
            is_router=False, workspace_id=workspace.id,
        )
        db.add(worker)
        db.commit()

        with patch('src.api.routes.chat.chat_stream') as mock_stream:
            # 模擬單代理串流回傳
            async def _fake_stream(*a, **kw):
                class _FakeResp:
                    async def __aiter__(self):
                        yield b'data: {"type":"text","delta":"hello"}\n\n'
                        yield b'data: {"type":"done","conversation_id":"fake"}\n\n'
                    body_iterator = __aiter__(None)
                resp = MagicMock()
                async def _iter():
                    yield b'data: {"type":"text","delta":"hello"}\n\n'
                    yield b'data: {"type":"done","conversation_id":"fake"}\n\n'
                resp.body_iterator = _iter()
                return resp
            mock_stream.side_effect = _fake_stream

            response = client.post(
                '/api/chat',
                headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'},
                json={'message': '你好'},
            )

        assert response.status_code == 200
        body = response.text
        assert 'orchestrator.plan' not in body
        assert 'orchestrator.done' not in body

    def test_public_not_ready_fallback_to_master(self, client, admin_token, db, workspace):
        """Given public 代理不可用，When POST /api/chat，Then 轉回 master 代理。"""
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        public = Agent(
            name='客服代理人', description='一般問題回覆', model_type='cloud',
            is_router=False, agent_class='public', enabled=True, workspace_id=workspace.id,
        )
        db.add(router)
        db.add(public)
        db.commit()
        db.refresh(router)
        db.refresh(public)

        with (
            patch('src.api.routes.chat._pick_worker_agent', return_value=(public, 'public_default_fallback')),
            patch('src.api.routes.chat._is_agent_ready_for_chat', return_value=False),
            patch('src.api.routes.chat.chat_stream') as mock_stream,
        ):
            async def _fake_stream(*a, **kw):
                resp = MagicMock()
                async def _iter():
                    yield b'data: {"type":"text","delta":"ok"}\n\n'
                    yield b'data: {"type":"done","conversation_id":"fake"}\n\n'
                resp.body_iterator = _iter()
                return resp
            mock_stream.side_effect = _fake_stream

            response = client.post(
                '/api/chat',
                headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'},
                json={'message': '你是誰？'},
            )

        assert response.status_code == 200
        assert 'public_not_ready_master_fallback' in response.text
        called_kwargs = mock_stream.call_args.kwargs
        assert called_kwargs['agent_id'] == str(router.id)

    def test_tasked_not_ready_fallback_to_master(self, client, admin_token, db, workspace):
        """Given tasked 代理不可用，When POST /api/chat，Then 轉回 master 代理。"""
        router = Agent(
            name='Router', description='主路由', model_type='cloud',
            is_router=True, agent_class='master', enabled=True, workspace_id=workspace.id,
        )
        tasked = Agent(
            name='MIS部門', description='內部IT與MIS協助', model_type='cloud',
            is_router=False, agent_class='tasked', enabled=True, workspace_id=workspace.id,
        )
        db.add(router)
        db.add(tasked)
        db.commit()
        db.refresh(router)
        db.refresh(tasked)

        with (
            patch('src.api.routes.chat._pick_worker_agent', return_value=(tasked, 'tasked_default_fallback')),
            patch('src.api.routes.chat._is_agent_ready_for_chat', return_value=False),
            patch('src.api.routes.chat.chat_stream') as mock_stream,
        ):
            async def _fake_stream(*a, **kw):
                resp = MagicMock()
                async def _iter():
                    yield b'data: {"type":"text","delta":"ok"}\n\n'
                    yield b'data: {"type":"done","conversation_id":"fake"}\n\n'
                resp.body_iterator = _iter()
                return resp
            mock_stream.side_effect = _fake_stream

            response = client.post(
                '/api/chat',
                headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'},
                json={'message': '你是誰？'},
            )

        assert response.status_code == 200
        assert 'tasked_not_ready_master_fallback' in response.text
        called_kwargs = mock_stream.call_args.kwargs
        assert called_kwargs['agent_id'] == str(router.id)


# ──────────────────────────────────────────────
# T071：整合測試 — 複合訊息觸發 orchestrator
# ──────────────────────────────────────────────

class TestMultiAgentOrchestrator:
    """驗證複合訊息走 orchestrator 路徑，DB 有正確 session/task 資料。"""

    def _setup_agents(self, db, workspace):
        router = Agent(
            name='Router', description='主路由代理', model_type='cloud',
            is_router=True, workspace_id=workspace.id,
        )
        db.add(router)
        w1 = Agent(
            name='銷售代理', description='處理銷售查詢', model_type='cloud',
            is_router=False, workspace_id=workspace.id,
        )
        db.add(w1)
        w2 = Agent(
            name='通知代理', description='發送通知', model_type='cloud',
            is_router=False, workspace_id=workspace.id,
        )
        db.add(w2)
        db.commit()
        db.refresh(router); db.refresh(w1); db.refresh(w2)
        return router, w1, w2

    def test_complex_message_creates_session_and_tasks(self, client, admin_token, db, workspace):
        """Given 複合訊息含複合信號，When POST /api/chat，
        Then DB 有 MultiAgentSession，且 SSE 含 orchestrator.plan + orchestrator.done。"""
        router, w1, w2 = self._setup_agents(db, workspace)

        fake_plan = {
            'multi': True,
            'tasks': [
                {'task': '查詢本月銷售', 'agent_id': str(w1.id), 'depends_on': None},
                {'task': '發通知給主管', 'agent_id': str(w2.id), 'depends_on': [0]},
            ],
        }

        async def _fake_subtask_stream(*, task_row, enriched_message, agent, db, current_user,
                                       task_index, total_tasks, completed_so_far, **kwargs):
            task_row.status = 'running'
            yield f'data: {json.dumps({"type": "agent.start", "task_index": task_index, "agent_name": agent.name})}\n\n'
            task_row.result_text = f'完成任務 {task_index}'
            task_row.status = 'done'
            yield f'data: {json.dumps({"type": "agent.done", "task_index": task_index, "ok": True})}\n\n'

        with (
            patch('src.api.routes.chat._classify_routing', return_value='multi'),
            patch('src.api.routes.chat._decompose_tasks', return_value=fake_plan),
            patch('src.api.routes.chat._run_subtask_stream', side_effect=_fake_subtask_stream),
            patch('src.api.routes.chat._synthesize_and_evaluate',
                  return_value=('整合後的回覆', True, '')),
        ):
            response = client.post(
                '/api/chat',
                headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'},
                json={'message': '請查詢銷售報表並且同時發通知給主管'},
            )

        assert response.status_code == 200
        body = response.text
        assert 'orchestrator.plan' in body
        assert 'orchestrator.done' in body

        # 驗證 DB 有 MultiAgentSession
        session = db.query(MultiAgentSession).first()
        assert session is not None
        assert session.user_message == '請查詢銷售報表並且同時發通知給主管'
        assert session.status == 'done'
        assert session.react_step == 1

        # 驗證有 2 個 MultiAgentTask
        tasks = db.query(MultiAgentTask).filter(
            MultiAgentTask.session_id == session.id
        ).all()
        assert len(tasks) == 2


# ──────────────────────────────────────────────
# T072：整合測試 — eval_ok=false 觸發重試
# ──────────────────────────────────────────────

class TestOrchestratorRetry:
    """驗證自評失敗時重試，react_step 正確累加。"""

    def test_eval_failure_triggers_retry_and_increments_react_step(
        self, client, admin_token, db, workspace
    ):
        """Given 第一輪自評 ok=False，第二輪 ok=True，When POST /api/chat，
        Then SSE 含 orchestrator.retry，DB session.react_step == 2。"""
        router = Agent(
            name='Router', description='路由', model_type='cloud',
            is_router=True, workspace_id=workspace.id,
        )
        db.add(router)
        w1 = Agent(
            name='A1', description='代理 A', model_type='cloud',
            is_router=False, workspace_id=workspace.id,
        )
        db.add(w1)
        w2 = Agent(
            name='A2', description='代理 B', model_type='cloud',
            is_router=False, workspace_id=workspace.id,
        )
        db.add(w2)
        db.commit()
        db.refresh(router); db.refresh(w1); db.refresh(w2)

        call_count = {'n': 0}
        fake_plan = {
            'multi': True,
            'tasks': [
                {'task': '子任務甲', 'agent_id': str(w1.id), 'depends_on': None},
                {'task': '子任務乙', 'agent_id': str(w2.id), 'depends_on': None},
            ],
        }

        def _fake_synthesize(*args, **kwargs):
            call_count['n'] += 1
            if call_count['n'] == 1:
                return ('初版回覆', False, '不夠完整')
            return ('最終回覆', True, '')

        async def _fake_subtask_stream(*, task_row, enriched_message, agent, db, current_user,
                                       task_index, total_tasks, completed_so_far, **kwargs):
            task_row.status = 'done'
            task_row.result_text = f'結果 {task_index}'
            yield f'data: {json.dumps({"type": "agent.done", "task_index": task_index, "ok": True})}\n\n'

        with (
            patch('src.api.routes.chat._classify_routing', return_value='multi'),
            patch('src.api.routes.chat._decompose_tasks', return_value=fake_plan),
            patch('src.api.routes.chat._run_subtask_stream', side_effect=_fake_subtask_stream),
            patch('src.api.routes.chat._synthesize_and_evaluate', side_effect=_fake_synthesize),
        ):
            response = client.post(
                '/api/chat',
                headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'},
                json={'message': '請查詢銷售報表並且同時發通知給主管'},
            )

        assert response.status_code == 200
        body = response.text
        assert 'orchestrator.retry' in body
        assert 'orchestrator.done' in body

        session = db.query(MultiAgentSession).first()
        assert session is not None
        assert session.react_step == 2
        assert session.status == 'done'


# ──────────────────────────────────────────────
# T074：失敗測試 — 子代理失敗後繼續
# ──────────────────────────────────────────────

class TestSubAgentFailure:
    """驗證子代理失敗時 agent.error 被發出，其餘任務繼續，最終有 orchestrator.done。"""

    def test_failed_subtask_yields_agent_error_but_continues(
        self, client, admin_token, db, workspace
    ):
        """Given 第一個子代理失敗，When POST /api/chat，
        Then SSE 含 agent.error，且仍有 orchestrator.done。"""
        router = Agent(
            name='Router', description='路由', model_type='cloud',
            is_router=True, workspace_id=workspace.id,
        )
        db.add(router)
        w1 = Agent(
            name='失敗代理', description='會失敗', model_type='cloud',
            is_router=False, workspace_id=workspace.id,
        )
        db.add(w1)
        w2 = Agent(
            name='成功代理', description='會成功', model_type='cloud',
            is_router=False, workspace_id=workspace.id,
        )
        db.add(w2)
        db.commit()
        db.refresh(router); db.refresh(w1); db.refresh(w2)

        fake_plan = {
            'multi': True,
            'tasks': [
                {'task': '子任務失敗', 'agent_id': str(w1.id), 'depends_on': None},
                {'task': '子任務成功', 'agent_id': str(w2.id), 'depends_on': None},
            ],
        }

        async def _fake_subtask_stream(*, task_row, enriched_message, agent, db, current_user,
                                       task_index, total_tasks, completed_so_far, **kwargs):
            if agent.name == '失敗代理':
                task_row.status = 'failed'
                task_row.error = '模擬失敗'
                yield f'data: {json.dumps({"type": "agent.error", "task_index": task_index, "error": "模擬失敗"})}\n\n'
            else:
                task_row.status = 'done'
                task_row.result_text = '成功輸出'
                yield f'data: {json.dumps({"type": "agent.done", "task_index": task_index, "ok": True})}\n\n'

        with (
            patch('src.api.routes.chat._classify_routing', return_value='multi'),
            patch('src.api.routes.chat._decompose_tasks', return_value=fake_plan),
            patch('src.api.routes.chat._run_subtask_stream', side_effect=_fake_subtask_stream),
            patch('src.api.routes.chat._synthesize_and_evaluate',
                  return_value=('部分合成回覆', True, '')),
        ):
            response = client.post(
                '/api/chat',
                headers={'Authorization': f'Bearer {admin_token}', 'Content-Type': 'application/json'},
                json={'message': '請查詢銷售報表並且同時發通知給主管'},
            )

        assert response.status_code == 200
        body = response.text
        assert 'agent.error' in body
        assert 'orchestrator.done' in body

        # 驗證 DB：session 狀態為 done（有成功任務），且有失敗 task
        session = db.query(MultiAgentSession).first()
        assert session is not None
        assert session.status == 'done'
        failed_tasks = db.query(MultiAgentTask).filter(
            MultiAgentTask.session_id == session.id,
            MultiAgentTask.status == 'failed',
        ).all()
        assert len(failed_tasks) == 1
