from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.models import AccessPermission, AccessRole, AccessRolePermission, Agent, UserRoleBinding


def _grant_private_agent_permission(*, db, user_id, private_agent_id, private_agent_name: str | None = None) -> None:
    if private_agent_name:
        permission_key = f'entity.agent.{private_agent_name}.execute'
    else:
        permission_key = f'entity.agent.{str(private_agent_id)}.execute'
    permission_row = db.query(AccessPermission).filter(AccessPermission.key == permission_key).first()
    if permission_row is None:
        permission_row = AccessPermission(key=permission_key)
        db.add(permission_row)
        db.flush()

    role_row = AccessRole(code='private_agent_executor', name='私有代理可用角色', enabled=True)
    db.add(role_row)
    db.flush()

    db.add(AccessRolePermission(role_id=role_row.id, permission_id=permission_row.id))
    db.add(UserRoleBinding(user_id=user_id, role_id=role_row.id))
    db.commit()


def test_public_agents_hides_private_without_permission(client, db, regular_user, regular_user_token, workspace):
    private_agent = Agent(
        name='私有報表代理',
        description='內部報表專用',
        model_type='cloud',
        agent_class='private',
        enabled=True,
        workspace_id=workspace.id,
    )
    public_agent = Agent(
        name='公眾客服代理',
        description='一般客服問答',
        model_type='cloud',
        agent_class='public',
        enabled=True,
        workspace_id=workspace.id,
    )
    db.add(private_agent)
    db.add(public_agent)
    db.commit()

    response = client.get('/api/agents/public', headers={'Authorization': f'Bearer {regular_user_token}'})

    assert response.status_code == 200
    agent_ids = {str(item['id']) for item in response.json().get('agents', [])}
    assert str(public_agent.id) in agent_ids
    assert str(private_agent.id) not in agent_ids


def test_public_agents_shows_private_with_permission(client, db, regular_user, regular_user_token, workspace):
    private_agent = Agent(
        name='私有報表代理',
        description='內部報表專用',
        model_type='cloud',
        agent_class='private',
        enabled=True,
        workspace_id=workspace.id,
    )
    db.add(private_agent)
    db.commit()
    db.refresh(private_agent)

    _grant_private_agent_permission(db=db, user_id=regular_user.id, private_agent_id=private_agent.id)

    response = client.get('/api/agents/public', headers={'Authorization': f'Bearer {regular_user_token}'})

    assert response.status_code == 200
    agent_ids = {str(item['id']) for item in response.json().get('agents', [])}
    assert str(private_agent.id) in agent_ids


def test_private_agent_chat_stream_forbidden_without_permission(client, db, regular_user_token, workspace):
    private_agent = Agent(
        name='私有法務代理',
        description='法務內部專用',
        model_type='cloud',
        agent_class='private',
        enabled=True,
        workspace_id=workspace.id,
    )
    db.add(private_agent)
    db.commit()

    response = client.get(
        f'/api/agents/{private_agent.id}/chat/stream',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        params={'message': '測試訊息'},
    )

    assert response.status_code == 403


def test_chat_entry_prefers_private_agent_with_permission(client, db, regular_user, regular_user_token, workspace):
    router = Agent(
        name='Router',
        description='主路由',
        model_type='cloud',
        agent_class='master',
        is_router=True,
        enabled=True,
        workspace_id=workspace.id,
    )
    private_agent = Agent(
        name='財務私有代理',
        description='內部財務分析',
        model_type='cloud',
        agent_class='private',
        enabled=True,
        workspace_id=workspace.id,
    )
    tasked_agent = Agent(
        name='一般任務代理',
        description='一般任務處理',
        model_type='cloud',
        agent_class='tasked',
        enabled=True,
        workspace_id=workspace.id,
    )
    db.add(router)
    db.add(private_agent)
    db.add(tasked_agent)
    db.commit()
    db.refresh(private_agent)

    _grant_private_agent_permission(db=db, user_id=regular_user.id, private_agent_id=private_agent.id)

    with patch('src.api.routes.chat.chat_stream') as mock_stream:
        async def _fake_stream(*args, **kwargs):
            response = MagicMock()

            async def _iter():
                yield b'data: {"type":"text","delta":"ok"}\n\n'
                yield b'data: {"type":"done","conversation_id":"fake"}\n\n'

            response.body_iterator = _iter()
            return response

        mock_stream.side_effect = _fake_stream

        response = client.post(
            '/api/chat',
            headers={'Authorization': f'Bearer {regular_user_token}', 'Content-Type': 'application/json'},
            json={'message': '請財務私有代理協助我整理報表'},
        )

    assert response.status_code == 200
    assert 'private_explicit_mention' in response.text
    called_agent_id = str(mock_stream.call_args.kwargs.get('agent_id') or '')
    assert called_agent_id == str(private_agent.id)


def test_public_agents_shows_private_with_name_permission_key(client, db, regular_user, regular_user_token, workspace):
    private_agent = Agent(
        name='財務部',
        description='財務內部代理',
        model_type='cloud',
        agent_class='private',
        enabled=True,
        workspace_id=workspace.id,
    )
    db.add(private_agent)
    db.commit()
    db.refresh(private_agent)

    _grant_private_agent_permission(
        db=db,
        user_id=regular_user.id,
        private_agent_id=private_agent.id,
        private_agent_name='財務部',
    )

    response = client.get('/api/agents/public', headers={'Authorization': f'Bearer {regular_user_token}'})

    assert response.status_code == 200
    agent_ids = {str(item['id']) for item in response.json().get('agents', [])}
    assert str(private_agent.id) in agent_ids


def test_chat_entry_prefers_private_even_without_explicit_mention(client, db, regular_user, regular_user_token, workspace):
    router = Agent(
        name='Router',
        description='主路由',
        model_type='cloud',
        agent_class='master',
        is_router=True,
        enabled=True,
        workspace_id=workspace.id,
    )
    private_agent = Agent(
        name='財務部',
        description='財務內部任務',
        model_type='cloud',
        agent_class='private',
        enabled=True,
        workspace_id=workspace.id,
    )
    public_agent = Agent(
        name='客服代理人',
        description='一般客服問答',
        model_type='cloud',
        agent_class='public',
        enabled=True,
        workspace_id=workspace.id,
    )
    db.add(router)
    db.add(private_agent)
    db.add(public_agent)
    db.commit()
    db.refresh(private_agent)

    _grant_private_agent_permission(
        db=db,
        user_id=regular_user.id,
        private_agent_id=private_agent.id,
        private_agent_name='財務部',
    )

    with (
        patch('src.api.routes.chat.chat_stream') as mock_stream,
        patch('src.api.routes.chat._resolve_router_assignment_mode', return_value='hybrid'),
    ):
        async def _fake_stream(*args, **kwargs):
            response = MagicMock()

            async def _iter():
                yield b'data: {"type":"text","delta":"ok"}\n\n'
                yield b'data: {"type":"done","conversation_id":"fake"}\n\n'

            response.body_iterator = _iter()
            return response

        mock_stream.side_effect = _fake_stream

        response = client.post(
            '/api/chat',
            headers={'Authorization': f'Bearer {regular_user_token}', 'Content-Type': 'application/json'},
            json={'message': '你是誰？使用什麼大模型？'},
        )

    assert response.status_code == 200
    assert 'private_' in response.text
    called_agent_id = str(mock_stream.call_args.kwargs.get('agent_id') or '')
    assert called_agent_id == str(private_agent.id)
