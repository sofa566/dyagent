import re


class TestFunctionsApi:
    def test_functions_admin_crud_and_selectable(self, client, admin_user, admin_token, regular_user, regular_user_token):
        create_enabled = client.post(
            '/api/functions',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'name': 'generic-strict',
                'provider': 'generic',
                'function_definition_template': '[[CALL tool=<allowed-tool-name>]]\n{}',
                'description': 'strict template',
                'enabled': True,
                'version': 1,
            },
        )
        assert create_enabled.status_code == 200
        assert create_enabled.json().get('function_definition_template')
        enabled_id = create_enabled.json()['id']

        create_disabled = client.post(
            '/api/functions',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'name': 'generic-disabled',
                'provider': 'generic',
                'function_definition_template': 'disabled',
                'enabled': False,
            },
        )
        assert create_disabled.status_code == 200

        list_all = client.get('/api/functions', headers={'Authorization': f'Bearer {admin_token}'})
        assert list_all.status_code == 200
        names = [item['name'] for item in list_all.json().get('functions', [])]
        assert 'generic-strict' in names
        assert 'generic-disabled' in names

        list_selectable = client.get('/api/functions/selectable', headers={'Authorization': f'Bearer {admin_token}'})
        assert list_selectable.status_code == 200
        selectable_names = [item['name'] for item in list_selectable.json().get('functions', [])]
        assert 'generic-strict' in selectable_names
        assert 'generic-disabled' not in selectable_names

        forbidden = client.get('/api/functions', headers={'Authorization': f'Bearer {regular_user_token}'})
        assert forbidden.status_code == 403

        delete_res = client.delete(f'/api/functions/{enabled_id}', headers={'Authorization': f'Bearer {admin_token}'})
        assert delete_res.status_code == 200


class TestRagBindingsApi:
    def test_private_dataset_cannot_bind_to_other_agent(self, client, db, workspace, admin_user, admin_token, agent):
        from src.models import Agent, RagDataset

        second_agent = Agent(
            name='Second Agent',
            description='Second Description',
            model_type='cloud',
            workspace_id=workspace.id,
        )
        db.add(second_agent)
        db.commit()
        db.refresh(second_agent)

        private_dataset_res = client.post(
            '/api/rag/datasets',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'name': 'agent-a-private-ds',
                'scope': 'agent_private',
                'agent_id': str(agent.id),
                'sensitivity': 'restricted',
                'enabled': True,
            },
        )
        assert private_dataset_res.status_code == 200
        private_dataset_id = private_dataset_res.json()['dataset']['id']
        assert private_dataset_res.json()['dataset']['agent_id'] == str(agent.id)

        global_dataset_res = client.post(
            '/api/rag/datasets',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'name': 'global-ds',
                'sensitivity': 'normal',
                'enabled': True,
            },
        )
        assert global_dataset_res.status_code == 200
        global_dataset_id = global_dataset_res.json()['dataset']['id']

        bind_ok = client.put(
            f'/api/agents/{agent.id}/rag/bindings',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'global_dataset_ids': [global_dataset_id],
                'private_dataset_ids': [private_dataset_id],
            },
        )
        assert bind_ok.status_code == 200

        bind_other_agent = client.put(
            f'/api/agents/{second_agent.id}/rag/bindings',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'private_dataset_ids': [private_dataset_id],
            },
        )
        assert bind_other_agent.status_code == 400

        rows = db.query(RagDataset).all()
        assert len(rows) >= 2

    def test_global_rag_dataset_admin_crud(self, client, admin_token, regular_user_token):
        create_res = client.post(
            '/api/rag/datasets',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'name': 'global-crud-ds',
                'sensitivity': 'normal',
                'vector_backend': 'qdrant',
                'index_name': 'idx_global_crud',
                'enabled': True,
            },
        )
        assert create_res.status_code == 200
        dataset_id = create_res.json()['dataset']['id']

        update_res = client.put(
            f'/api/rag/datasets/{dataset_id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'name': 'global-crud-ds-updated',
                'sensitivity': 'confidential',
                'enabled': False,
            },
        )
        assert update_res.status_code == 200
        updated = update_res.json().get('dataset', {})
        assert updated.get('name') == 'global-crud-ds-updated'
        assert updated.get('sensitivity') == 'confidential'
        assert updated.get('enabled') is False

        forbidden_update = client.put(
            f'/api/rag/datasets/{dataset_id}',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            json={'name': 'should-fail'},
        )
        assert forbidden_update.status_code == 403

        delete_res = client.delete(
            f'/api/rag/datasets/{dataset_id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert delete_res.status_code == 200

        not_found_delete = client.delete(
            f'/api/rag/datasets/{dataset_id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert not_found_delete.status_code == 404

    def test_global_rag_dataset_upload_document(self, client, admin_token):
        create_res = client.post(
            '/api/rag/datasets',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'name': 'global-upload-ds', 'enabled': True},
        )
        assert create_res.status_code == 200
        dataset_id = create_res.json()['dataset']['id']

        upload_res = client.post(
            f'/api/rag/datasets/{dataset_id}/upload',
            headers={'Authorization': f'Bearer {admin_token}'},
            files={'file': ('kb.txt', b'RAG upload content for global dataset', 'text/plain')},
        )
        assert upload_res.status_code == 200
        payload = upload_res.json()
        assert payload.get('ok') is True
        assert payload.get('dataset_id') == dataset_id
        assert payload.get('status') in {'ready', 'failed', 'uploaded'}


class TestRouterAndLlmTurn:
    def test_chat_entry_router_persists_routing_events_and_llm_turn(self, client, db, admin_user, admin_token, agent, monkeypatch):
        from src.api.routes import chat as chat_routes
        from src.models.events import EventPart

        async def fake_stream_complete_async(llm_client, *, prompt: str, tier: str | None):
            _ = llm_client, prompt, tier
            yield '這是測試回覆。'

        monkeypatch.setattr(chat_routes, '_stream_complete_async', fake_stream_complete_async)

        response = client.post(
            '/api/chat',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'message': '請幫我處理測試請求'},
        )
        assert response.status_code == 200
        body = response.text
        assert 'route.decision' in body
        assert '"type":"done"' in body or '"type": "done"' in body

        match = re.search(r'"conversation_id":"([a-f0-9-]{36})"', body)
        assert match is not None
        conversation_id = match.group(1)

        routing_events_res = client.get(
            f'/api/conversations/{conversation_id}/routing-events',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert routing_events_res.status_code == 200
        event_types = [item['type'] for item in routing_events_res.json().get('events', [])]
        assert 'route.decision' in event_types
        assert 'route.forward' in event_types

        event_rows = db.query(EventPart).filter(EventPart.conversation_id == conversation_id).all()
        assert len(event_rows) >= 2

    def test_chat_entry_router_regular_user_can_chat_without_agent_selection(
        self,
        client,
        regular_user,
        regular_user_token,
        agent,
        monkeypatch,
    ):
        from src.api.routes import chat as chat_routes

        async def fake_stream_complete_async(llm_client, *, prompt: str, tier: str | None):
            _ = llm_client, prompt, tier
            yield '一般使用者走統一入口成功。'

        monkeypatch.setattr(chat_routes, '_stream_complete_async', fake_stream_complete_async)

        response = client.post(
            '/api/chat',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            json={'message': '哈囉，請直接回覆'},
        )
        assert response.status_code == 200
        body = response.text
        assert 'route.decision' in body
        assert 'conversation_id' in body
        assert '"type":"done"' in body or '"type": "done"' in body

    def test_chat_stream_records_tool_error_status(self, client, db, admin_user, admin_token, agent, monkeypatch):
        from src.api.routes import chat as chat_routes
        from src.models import Conversation, LlmTurn, Message
        from src.services.chat_router import ChatRouter

        async def fake_stream_complete_async(llm_client, *, prompt: str, tier: str | None):
            _ = llm_client, prompt, tier
            yield '[[CALL tool=weather_tool]]\n{"q":"台北"}'

        async def fake_call_tool_async(self, *, session_id: str, tool: str, payload: dict, db, agent_id: str):
            _ = self, session_id, tool, payload, db, agent_id
            return {'ok': False, 'error': 'tool_unavailable'}

        monkeypatch.setattr(chat_routes, '_stream_complete_async', fake_stream_complete_async)
        monkeypatch.setattr(ChatRouter, 'call_tool_async', fake_call_tool_async)

        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'message': '請查詢台北天氣'},
        )
        assert response.status_code == 200

        body = response.text
        assert 'conversation_id' in body

        conversation = db.query(Conversation).filter(Conversation.agent_id == agent.id).order_by(Conversation.created_at.desc()).first()
        assert conversation is not None
        user_message = db.query(Message).filter(Message.conversation_id == conversation.id, Message.role == 'user').order_by(Message.timestamp.desc()).first()
        assert user_message is not None

        chat_routes._create_llm_turn_record(
            db=db,
            conversation_id=str(conversation.id),
            agent_id=str(agent.id),
            user_message_id=str(user_message.id),
            assistant_message_id=None,
            provider='test-provider',
            model='test-model',
            tier='cloud',
            system_prompt_snapshot='prompt',
            context_snapshot={'entry': 'unit-test'},
            usage={},
            cost_usd=None,
            latency_ms=10,
            status='tool_error',
            error='tool_unavailable',
        )

        turn_row = db.query(LlmTurn).filter(LlmTurn.conversation_id == conversation.id).order_by(LlmTurn.created_at.desc()).first()
        assert turn_row is not None
        assert turn_row.status == 'tool_error'
        assert 'tool_unavailable' in (turn_row.error or '')

    def test_chat_stream_skips_intent_shortcut_when_selected_dataset_has_rag_hits(
        self,
        client,
        admin_token,
        agent,
        monkeypatch,
    ):
        """Given 使用者指定資料集且 RAG 命中，When 偵測到技能意圖，Then 不走 intent shortcut 直接保留 LLM 回覆路徑。"""
        from src.api.routes import chat as chat_routes
        from src.services.chat_router import ChatRouter

        async def fake_stream_complete_async(llm_client, *, prompt: str, tier: str | None):
            _ = llm_client, prompt, tier
            yield 'RAG 一般回覆路徑'

        def fake_prepare_integrations(self, *, db, agent_id: str):
            _ = db, agent_id
            self._allowed_tools = {'weather_tool'}
            return {
                'skills': ['weather_tool'],
                'mcp': [],
                'rag': {'enabled': True, 'sources': ['test-ds'], 'topK': 5},
            }

        async def fail_if_tool_called(self, *, session_id: str, tool: str, payload: dict, db, agent_id: str):
            _ = self, session_id, tool, payload, db, agent_id
            raise RuntimeError('intent shortcut should be skipped when rag hits > 0')

        monkeypatch.setattr(chat_routes, '_stream_complete_async', fake_stream_complete_async)
        monkeypatch.setattr(chat_routes, '_detect_intent_skill_name', lambda **kwargs: 'weather_tool')
        monkeypatch.setattr(
            chat_routes,
            '_build_chat_rag_context',
            lambda **kwargs: ('[RAG]\n- 命中測試片段', {'reason': 'ok', 'hits': 3, 'selected_rows': [{'text': 'RASA'}]}),
        )
        monkeypatch.setattr(ChatRouter, '_prepare_integrations', fake_prepare_integrations)
        monkeypatch.setattr(ChatRouter, 'call_tool_async', fail_if_tool_called)

        message_with_dataset_context = (
            '請在資料集找 MCP server 並回答\n\n'
            '[附加輸入]\n'
            '使用資料集:\n'
            '- mis_pb (id=0052dc8c-4dfc-4cff-b812-aca0f32b051d, scope=global)'
        )

        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={
                'message': message_with_dataset_context,
                'selected_dataset_ids': '0052dc8c-4dfc-4cff-b812-aca0f32b051d',
            },
        )

        assert response.status_code == 200
        body = response.text
        assert 'RAG 一般回覆路徑' in body
        assert 'tool_start' not in body

    def test_chat_stream_rag_then_tool_injects_evidence_payload(
        self,
        client,
        admin_token,
        agent,
        monkeypatch,
    ):
        """Given 資料集檢索與產出報表需求並存，When 走 intent shortcut，Then payload 需帶入 RAG evidence。"""
        from src.api.routes import chat as chat_routes
        from src.services.chat_router import ChatRouter

        captured_payloads: list[dict] = []

        async def fake_stream_complete_async(llm_client, *, prompt: str, tier: str | None):
            _ = llm_client, prompt, tier
            yield '工具執行後一般回覆'

        def fake_prepare_integrations(self, *, db, agent_id: str):
            _ = db, agent_id
            self._allowed_tools = {'docx_report_tool'}
            return {
                'skills': ['docx_report_tool'],
                'mcp': [],
                'rag': {'enabled': True, 'sources': ['test-ds'], 'topK': 5},
            }

        async def fake_call_tool_async(self, *, session_id: str, tool: str, payload: dict, db, agent_id: str):
            _ = self, session_id, db, agent_id
            captured_payloads.append({'tool': tool, 'payload': payload})
            return {'ok': True, 'result': {'output': 'docx_done'}}

        monkeypatch.setattr(chat_routes, '_stream_complete_async', fake_stream_complete_async)
        monkeypatch.setattr(chat_routes, '_detect_intent_skill_name', lambda **kwargs: 'docx_report_tool')
        monkeypatch.setattr(
            chat_routes,
            '_build_chat_rag_context',
            lambda **kwargs: (
                '[RAG]\n- 命中測試片段',
                {
                    'reason': 'ok',
                    'hits': 2,
                    'selected_rows': [
                        {
                            'dataset_id': '0052dc8c-4dfc-4cff-b812-aca0f32b051d',
                            'dataset_name': 'mis_pb',
                            'filename': 'rasa-note.txt',
                            'page_number': 1,
                            'snippet': 'RASA server support MCP integration',
                            'score': 0.91,
                        }
                    ],
                },
            ),
        )
        monkeypatch.setattr(ChatRouter, '_prepare_integrations', fake_prepare_integrations)
        monkeypatch.setattr(ChatRouter, 'call_tool_async', fake_call_tool_async)

        message_with_dataset_context = (
            '請先搜尋 RASA 再產出 docx 報表\n\n'
            '[附加輸入]\n'
            '使用資料集:\n'
            '- mis_pb (id=0052dc8c-4dfc-4cff-b812-aca0f32b051d, scope=global)'
        )

        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={
                'message': message_with_dataset_context,
                'selected_dataset_ids': '0052dc8c-4dfc-4cff-b812-aca0f32b051d',
            },
        )

        assert response.status_code == 200
        assert captured_payloads
        payload = captured_payloads[0]['payload']
        rag_obj = payload.get('_rag') if isinstance(payload, dict) else None
        assert captured_payloads[0]['tool'] == 'docx_report_tool'
        assert isinstance(rag_obj, dict)
        assert isinstance(rag_obj.get('evidence'), list)
        assert len(rag_obj.get('evidence') or []) == 1
        assert rag_obj.get('evidence')[0].get('text') == 'RASA server support MCP integration'

    def test_chat_stream_injects_rag_runtime_guard_when_hits_exist(
        self,
        client,
        admin_token,
        agent,
        monkeypatch,
    ):
        """Given RAG 命中片段，When 組裝主 prompt，Then 注入禁止誤稱無法存取資料集的 guard。"""
        from src.api.routes import chat as chat_routes
        from src.services.chat_router import ChatRouter

        captured_prompts: list[str] = []

        async def fake_stream_complete_async(llm_client, *, prompt: str, tier: str | None):
            _ = llm_client, tier
            captured_prompts.append(prompt)
            yield '回覆'

        def fake_prepare_integrations(self, *, db, agent_id: str):
            _ = db, agent_id
            self._allowed_tools = set()
            return {
                'skills': [],
                'mcp': [],
                'rag': {'enabled': False, 'sources': [], 'topK': 5},
            }

        monkeypatch.setattr(chat_routes, '_stream_complete_async', fake_stream_complete_async)
        monkeypatch.setattr(
            chat_routes,
            '_build_chat_rag_context',
            lambda **kwargs: ('[RAG Context]\n- 測試片段', {'reason': 'ok', 'hits': 1, 'selected_rows': [{'snippet': 'MCP server'}]}),
        )
        monkeypatch.setattr(ChatRouter, '_prepare_integrations', fake_prepare_integrations)

        message_with_dataset_context = (
            '請使用我選擇的資料集，搜尋 MCP server\n\n'
            '[附加輸入]\n'
            '使用資料集:\n'
            '- mis_pb (id=0052dc8c-4dfc-4cff-b812-aca0f32b051d, scope=global)'
        )

        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'message': message_with_dataset_context},
        )

        assert response.status_code == 200
        assert captured_prompts
        assert any('[RAG Runtime Guard]' in prompt for prompt in captured_prompts)
        assert any('不得宣稱「無法存取資料集」' in prompt for prompt in captured_prompts)

    def test_chat_stream_plain_rag_reply_appends_clickable_citations(
        self,
        client,
        admin_token,
        agent,
        monkeypatch,
    ):
        """Given 一般 RAG 回覆未走工具，When 串流結束，Then 追加可點擊來源連結。"""
        from src.api.routes import chat as chat_routes
        from src.services.chat_router import ChatRouter

        async def fake_stream_complete_async(llm_client, *, prompt: str, tier: str | None):
            _ = llm_client, prompt, tier
            yield '已依資料集找到 MCP server 相關內容。'

        def fake_prepare_integrations(self, *, db, agent_id: str):
            _ = db, agent_id
            self._allowed_tools = set()
            return {
                'skills': [],
                'mcp': [],
                'rag': {'enabled': False, 'sources': [], 'topK': 5},
            }

        monkeypatch.setattr(chat_routes, '_stream_complete_async', fake_stream_complete_async)
        monkeypatch.setattr(ChatRouter, '_prepare_integrations', fake_prepare_integrations)
        monkeypatch.setattr(
            chat_routes,
            '_build_chat_rag_context',
            lambda **kwargs: (
                '[RAG Context]\n- 命中測試片段',
                {
                    'reason': 'ok',
                    'hits': 1,
                    'selected_rows': [
                        {
                            'dataset_id': '0052dc8c-4dfc-4cff-b812-aca0f32b051d',
                            'filename': '33-1_回答MCP問題.docx',
                            'file_key': 'abc123_33-1_回答MCP問題.docx',
                            'page_number': None,
                            'snippet': 'MCP server text',
                        }
                    ],
                },
            ),
        )

        message_with_dataset_context = (
            '請使用我選擇的資料集，搜尋 MCP server\n\n'
            '[附加輸入]\n'
            '使用資料集:\n'
            '- mis_pb (id=0052dc8c-4dfc-4cff-b812-aca0f32b051d, scope=global)'
        )

        response = client.get(
            f'/api/agents/{agent.id}/chat/stream',
            headers={'Authorization': f'Bearer {admin_token}'},
            params={'message': message_with_dataset_context},
        )

        assert response.status_code == 200
        body = response.text
        assert '參考來源' in body
        assert '/api/rag/datasets/0052dc8c-4dfc-4cff-b812-aca0f32b051d/documents/abc123_33-1_%E5%9B%9E%E7%AD%94MCP%E5%95%8F%E9%A1%8C.docx/open' in body


class TestLlmUsageAndCost:
    def test_normalize_usage_payload_and_estimate_cost(self):
        from src.api.routes import chat as chat_routes

        usage = chat_routes._normalize_usage_payload(
            usage_raw={'prompt_tokens': 1000, 'completion_tokens': 500},
            user_text='ignored',
            assistant_text='ignored',
        )
        assert usage['input_tokens'] == 1000
        assert usage['output_tokens'] == 500
        assert usage['total_tokens'] == 1500

        cost = chat_routes._estimate_cost_usd(
            provider='openai',
            model='gpt-4o',
            usage=usage,
            route_info={},
        )
        assert cost is not None
        assert float(cost) > 0

    def test_estimate_cost_prefers_direct_cost_from_route_info(self):
        from src.api.routes import chat as chat_routes

        usage = chat_routes._normalize_usage_payload(
            usage_raw={'prompt_tokens': 200, 'completion_tokens': 100},
            user_text='ignored',
            assistant_text='ignored',
        )
        cost = chat_routes._estimate_cost_usd(
            provider='openai',
            model='gpt-4o',
            usage=usage,
            route_info={'cost': 0.012345},
        )
        assert cost is not None
        assert float(cost) == 0.012345

    def test_estimate_cost_uses_custom_cost_table_json(self, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(
            chat_routes.settings,
            'LLM_COST_TABLE_JSON',
            '{"openai:gpt-4o": {"input_per_1k": 0.01, "output_per_1k": 0.02}}',
            raising=False,
        )
        chat_routes._load_cost_table_from_settings.cache_clear()

        usage = chat_routes._normalize_usage_payload(
            usage_raw={'prompt_tokens': 1000, 'completion_tokens': 1000},
            user_text='ignored',
            assistant_text='ignored',
        )
        cost = chat_routes._estimate_cost_usd(
            provider='openai',
            model='gpt-4o',
            usage=usage,
            route_info={},
        )
        assert cost is not None
        assert float(cost) == 0.03

        monkeypatch.setattr(chat_routes.settings, 'LLM_COST_TABLE_JSON', '', raising=False)
        chat_routes._load_cost_table_from_settings.cache_clear()

    def test_estimate_cost_invalid_cost_table_json_falls_back_default(self, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(chat_routes.settings, 'LLM_COST_TABLE_JSON', '{invalid-json', raising=False)
        chat_routes._load_cost_table_from_settings.cache_clear()

        usage = chat_routes._normalize_usage_payload(
            usage_raw={'prompt_tokens': 1000, 'completion_tokens': 1000},
            user_text='ignored',
            assistant_text='ignored',
        )
        cost = chat_routes._estimate_cost_usd(
            provider='openai',
            model='gpt-4o',
            usage=usage,
            route_info={},
        )
        assert cost is not None
        assert float(cost) > 0

        monkeypatch.setattr(chat_routes.settings, 'LLM_COST_TABLE_JSON', '', raising=False)
        chat_routes._load_cost_table_from_settings.cache_clear()

    def test_load_cost_table_invalid_schema_returns_empty(self, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(chat_routes.settings, 'LLM_COST_TABLE_JSON', '[1,2,3]', raising=False)
        chat_routes._load_cost_table_from_settings.cache_clear()

        table = chat_routes._load_cost_table_from_settings()
        assert table == {}

        monkeypatch.setattr(chat_routes.settings, 'LLM_COST_TABLE_JSON', '', raising=False)
        chat_routes._load_cost_table_from_settings.cache_clear()

    def test_load_cost_table_skips_invalid_items_and_keeps_valid(self, monkeypatch):
        from src.api.routes import chat as chat_routes

        monkeypatch.setattr(
            chat_routes.settings,
            'LLM_COST_TABLE_JSON',
            '{"broken":{"input_per_1k":1},"openai:gpt-4o":{"input_per_1k":0.01,"output_per_1k":0.02}}',
            raising=False,
        )
        chat_routes._load_cost_table_from_settings.cache_clear()

        table = chat_routes._load_cost_table_from_settings()
        assert ('openai', 'gpt-4o') in table
        assert ('broken', '') not in table

        monkeypatch.setattr(chat_routes.settings, 'LLM_COST_TABLE_JSON', '', raising=False)
        chat_routes._load_cost_table_from_settings.cache_clear()
