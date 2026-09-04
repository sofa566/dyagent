from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.api.routes import line_integration


def test_line_webhook_creates_session_and_agent_reply(client, admin_token, agent, monkeypatch):
    # 目的：驗證 LINE webhook 進站後會建立 session 並在 bot 模式寫入 AI 回覆。
    # 為什麼：LINE 通道最核心流程是入站訊息 -> 系統回覆，需有回歸測試避免中斷。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')

    with patch.object(line_integration, '_run_agent_reply', return_value='您好，這是系統回覆'):
        response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'message',
                        'source': {'type': 'user', 'userId': 'U_LINE_TEST_001'},
                        'message': {'id': 'msg-001', 'type': 'text', 'text': '你好'},
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json().get('processed') == 1

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    assert sessions_response.status_code == 200
    sessions = sessions_response.json().get('sessions') or []
    assert len(sessions) == 1
    assert sessions[0].get('line_user_id') == 'U_LINE_TEST_001'

    session_id = sessions[0].get('id')
    messages_response = client.get(
        f'/api/line/sessions/{session_id}/messages?limit=20',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert messages_response.status_code == 200
    messages = messages_response.json().get('messages') or []
    assert len(messages) == 2
    assert messages[0].get('direction') == 'inbound'
    assert messages[1].get('direction') == 'outbound'
    assert messages[1].get('sender_type') == 'agent'


def test_line_operator_can_switch_human_mode_and_push_message(client, admin_token, agent, monkeypatch):
    # 目的：驗證操作員可切換 human 模式並主動推送訊息給 LINE 使用者。
    # 為什麼：人工接手是 LINE 客服場景關鍵，需確保 mode 與 push API 可用。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')

    with patch.object(line_integration, '_run_agent_reply', return_value='初始 AI 回覆'):
        first_response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'message',
                        'source': {'type': 'user', 'userId': 'U_LINE_TEST_002'},
                        'message': {'id': 'msg-002', 'type': 'text', 'text': '請人工客服聯絡我'},
                    }
                ]
            },
        )
    assert first_response.status_code == 200

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    session_id = session.get('id')

    mode_response = client.put(
        f'/api/line/sessions/{session_id}/mode',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={'mode': 'human'},
    )
    assert mode_response.status_code == 200
    assert mode_response.json().get('mode') == 'human'

    with patch.object(line_integration, '_push_line_message', new=AsyncMock(return_value=None)):
        send_response = client.post(
            f'/api/line/sessions/{session_id}/messages',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'text': '您好，我是客服，已收到您的需求。'},
        )
    assert send_response.status_code == 200
    assert send_response.json().get('sent') is True


def test_line_webhook_rejects_invalid_signature(client, agent, monkeypatch):
    # 目的：驗證簽章驗證啟用時，無效簽章會被拒絕。
    # 為什麼：webhook 是外部入口，需確保未授權請求無法觸發對話流程。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', True)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_SECRET', 'line-secret-for-test')

    response = client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_TEST_003'},
                    'message': {'id': 'msg-003', 'type': 'text', 'text': 'hello'},
                }
            ]
        },
        headers={'X-Line-Signature': 'invalid-signature'},
    )

    assert response.status_code == 403
