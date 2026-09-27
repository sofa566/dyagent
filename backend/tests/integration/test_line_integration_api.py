from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.api.routes import line_integration
from src.models import Agent, LineChannelSession, MonitoringRecord, RenalPatient
from src.services.redis_service import redis_service


def test_line_webhook_first_message_triggers_binding_prompt(client, admin_token, agent, monkeypatch):
    # 目的：驗證首次 LINE 進站訊息會觸發姓名綁定提示。
    # 為什麼：自動綁定為 LINE 入口前置流程，需確保在 bot 回覆前先完成身份收斂。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_send_follow_welcome', AsyncMock(return_value=True))
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_send_follow_welcome', AsyncMock(return_value=True))

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
    assert messages[1].get('sender_type') == 'system'
    assert '姓名' in str(messages[1].get('content') or '')


def test_line_webhook_follow_event_starts_onboarding(client, admin_token, agent, monkeypatch):
    # 目的：驗證 follow 事件可立即建立 session 並啟動綁定流程。
    # 為什麼：新好友可能尚未主動傳訊，需在 follow 當下啟動 onboarding。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))
    line_user_id = f'U_LINE_FOLLOW_{uuid.uuid4().hex[:8]}'

    with patch.object(line_integration, '_push_line_message', new=AsyncMock(return_value=None)):
        response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'follow',
                        'webhookEventId': 'evt-follow-001',
                        'source': {'type': 'user', 'userId': line_user_id},
                    }
                ]
            },
        )

    assert response.status_code == 200
    assert response.json().get('processed') == 1

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    assert session.get('line_user_id') == line_user_id
    assert session.get('binding_status') == 'awaiting_name'

    messages_response = client.get(
        f"/api/line/sessions/{session.get('id')}/messages?limit=20",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    messages = messages_response.json().get('messages') or []
    assert len(messages) == 1
    assert messages[0].get('direction') == 'outbound'
    assert messages[0].get('sender_type') == 'system'
    assert '姓名' in str(messages[0].get('content') or '')


def test_line_webhook_follow_duplicate_event_idempotent(client, admin_token, agent, monkeypatch):
    # 目的：驗證 follow 事件重送時只會被處理一次。
    # 為什麼：LINE webhook 可能重試，若未去重會產生重複 welcome 與重複稽核資料。
    if redis_service.client is None:
        pytest.skip('Redis 未啟用，略過 webhook 去重測試')

    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    webhook_event_id = f'evt-follow-dup-{uuid.uuid4().hex}'
    line_user_id = f'U_LINE_FOLLOW_DUP_{uuid.uuid4().hex[:8]}'

    with patch.object(line_integration, '_push_line_message', new=AsyncMock(return_value=None)):
        first_response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                            'type': 'follow',
                            'webhookEventId': webhook_event_id,
                            'source': {'type': 'user', 'userId': line_user_id},
                    }
                ]
            },
        )
        second_response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                            'type': 'follow',
                            'webhookEventId': webhook_event_id,
                            'source': {'type': 'user', 'userId': line_user_id},
                    }
                ]
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json().get('processed') == 1
    assert second_response.json().get('processed') == 0

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    messages_response = client.get(
        f"/api/line/sessions/{session.get('id')}/messages?limit=20",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    messages = messages_response.json().get('messages') or []
    assert len(messages) == 1


def test_line_webhook_unfollow_sets_session_inactive(client, agent, db, monkeypatch):
    # 目的：驗證 unfollow 事件會將 LINE session 標示為 inactive。
    # 為什麼：使用者退訂後不應維持可互動狀態，且需保留歷史資料供稽核。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'unfollow',
                    'webhookEventId': 'evt-unfollow-001',
                    'source': {'type': 'user', 'userId': 'U_LINE_UNFOLLOW_001'},
                }
            ]
        },
    )

    session = db.query(LineChannelSession).filter(LineChannelSession.line_user_id == 'U_LINE_UNFOLLOW_001').first()
    assert session is None

    with patch.object(line_integration, '_push_line_message', new=AsyncMock(return_value=None)):
        client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'follow',
                        'webhookEventId': 'evt-unfollow-002',
                        'source': {'type': 'user', 'userId': 'U_LINE_UNFOLLOW_001'},
                    }
                ]
            },
        )

    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'unfollow',
                    'webhookEventId': 'evt-unfollow-003',
                    'source': {'type': 'user', 'userId': 'U_LINE_UNFOLLOW_001'},
                }
            ]
        },
    )

    session = db.query(LineChannelSession).filter(LineChannelSession.line_user_id == 'U_LINE_UNFOLLOW_001').first()
    assert session is not None
    assert str(session.status or '') == 'inactive'


def test_line_webhook_auto_binds_existing_renal_patient(client, admin_token, agent, db, monkeypatch):
    # 目的：驗證 LINE 綁定流程可用姓名與手機對應既有腎友主檔。
    # 為什麼：避免重複建立病患資料並確保後續訊息落在既有主檔。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    db.add(
        RenalPatient(
            patient_code='P998',
            display_name='王美華',
            tel_no='0911222333',
            line_user_id=None,
            enabled=True,
        )
    )
    db.commit()

    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_BIND_001'},
                    'message': {'id': 'bind-001', 'type': 'text', 'text': '你好'},
                }
            ]
        },
    )
    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_BIND_001'},
                    'message': {'id': 'bind-002', 'type': 'text', 'text': '王美華'},
                }
            ]
        },
    )
    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_BIND_001'},
                    'message': {'id': 'bind-003', 'type': 'text', 'text': '0911-222-333'},
                }
            ]
        },
    )

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    sessions = sessions_response.json().get('sessions') or []
    session = sessions[0]
    assert session.get('binding_status') == 'bound'
    assert (session.get('bound_patient') or {}).get('patient_id') == 'P998'


def test_line_webhook_assigns_renal_companion_agent(client, admin_token, agent, workspace, db, monkeypatch):
    # 目的：驗證 LINE 會話會固定指派到「腎友陪伴」代理。
    # 為什麼：腎友 LINE 訊息不可回退到一般主代理分派流程。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_RENAL_COMPANION_AGENT_NAME', '腎友陪伴')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    renal_companion_agent = Agent(
        name='腎友陪伴',
        description='腎友 LINE 陪伴代理',
        model_type='cloud',
        workspace_id=workspace.id,
        enabled=True,
    )
    db.add(renal_companion_agent)
    db.commit()

    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_RENAL_AGENT_001'},
                    'message': {'id': f'renal-agent-{uuid.uuid4().hex}', 'type': 'text', 'text': '你好'},
                }
            ]
        },
    )

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    assert session.get('assigned_agent_id') == str(renal_companion_agent.id)


def test_line_webhook_monitoring_report_replies_and_saves_record(client, admin_token, agent, db, monkeypatch):
    # 目的：驗證已綁定腎友傳送血壓/體重時，系統會即時確認並寫入監測紀錄。
    # 為什麼：回報流程需要穩定的可預期回覆，不能只依賴自由聊天模型。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    db.add(
        RenalPatient(
            patient_code='P200',
            display_name='測試腎友',
            tel_no='0911000222',
            line_user_id='U_LINE_REPORT_001',
            enabled=True,
            is_diabetic=False,
            dry_weight_kg=62.0,
        )
    )
    db.commit()

    with patch.object(line_integration, '_reply_line_message', new=AsyncMock(return_value=None)):
        response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'message',
                        'source': {'type': 'user', 'userId': 'U_LINE_REPORT_001'},
                        'replyToken': 'reply-report-001',
                        'message': {'id': 'report-001', 'type': 'text', 'text': '血壓 128/76 體重 63.4'},
                    }
                ]
            },
        )
    assert response.status_code == 200

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    messages_response = client.get(
        f"/api/line/sessions/{session.get('id')}/messages?limit=20",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    messages = messages_response.json().get('messages') or []
    assert any('收到你的血壓128/76，體重 63.4kg，紀錄完成。' in str(message.get('content') or '') for message in messages)

    record_count = db.query(MonitoringRecord).count()
    assert record_count >= 1


def test_line_webhook_monitoring_report_supports_bp_with_plain_weight(client, admin_token, agent, db, monkeypatch):
    # 目的：驗證「114/74，70.9」格式可直接入庫。
    # 為什麼：病患常以逗號分隔的簡寫回報，需避免被誤判為非結構資料。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    db.add(
        RenalPatient(
            patient_code='P201',
            display_name='純數字體重腎友',
            tel_no='0911333444',
            line_user_id='U_LINE_REPORT_002',
            enabled=True,
            is_diabetic=False,
            dry_weight_kg=61.0,
        )
    )
    db.commit()

    with patch.object(line_integration, '_reply_line_message', new=AsyncMock(return_value=None)):
        response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'message',
                        'source': {'type': 'user', 'userId': 'U_LINE_REPORT_002'},
                        'replyToken': 'reply-report-002',
                        'message': {'id': 'report-002', 'type': 'text', 'text': '114/74，70.9'},
                    }
                ]
            },
        )
    assert response.status_code == 200

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    messages_response = client.get(
        f"/api/line/sessions/{session.get('id')}/messages?limit=20",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    messages = messages_response.json().get('messages') or []
    assert any('收到你的血壓114/74，體重 70.9kg，紀錄完成。' in str(message.get('content') or '') for message in messages)

    saved_records = db.query(MonitoringRecord).all()
    assert len(saved_records) >= 1
    assert float((saved_records[0].measurements or {}).get('weight_kg') or 0) == 70.9


def test_line_webhook_monitoring_report_supports_split_messages(client, admin_token, agent, db, monkeypatch):
    # 目的：驗證分兩則傳送可累積欄位並完成入庫。
    # 為什麼：真實場景常先傳血壓再補體重，流程需容忍非一次填滿。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    db.add(
        RenalPatient(
            patient_code='P202',
            display_name='分開傳腎友',
            tel_no='0911555666',
            line_user_id='U_LINE_REPORT_003',
            enabled=True,
            is_diabetic=False,
            dry_weight_kg=64.0,
        )
    )
    db.commit()

    with patch.object(line_integration, '_reply_line_message', new=AsyncMock(return_value=None)):
        first_response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'message',
                        'source': {'type': 'user', 'userId': 'U_LINE_REPORT_003'},
                        'replyToken': 'reply-report-003-1',
                        'message': {'id': 'report-003-1', 'type': 'text', 'text': '114/74'},
                    }
                ]
            },
        )
        second_response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'message',
                        'source': {'type': 'user', 'userId': 'U_LINE_REPORT_003'},
                        'replyToken': 'reply-report-003-2',
                        'message': {'id': 'report-003-2', 'type': 'text', 'text': '70.9'},
                    }
                ]
            },
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    messages_response = client.get(
        f"/api/line/sessions/{session.get('id')}/messages?limit=30",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    messages = messages_response.json().get('messages') or []
    assert any('已收到部分資料，還缺 體重' in str(message.get('content') or '') for message in messages)
    assert any('收到你的血壓114/74，體重 70.9kg，紀錄完成。' in str(message.get('content') or '') for message in messages)

    saved_records = db.query(MonitoringRecord).all()
    assert len(saved_records) == 1
    saved_measurements = saved_records[0].measurements or {}
    assert int(saved_measurements.get('systolic') or 0) == 114
    assert int(saved_measurements.get('diastolic') or 0) == 74
    assert float(saved_measurements.get('weight_kg') or 0) == 70.9


def test_line_webhook_agent_reply_can_backfill_monitoring_record(client, admin_token, agent, db, monkeypatch):
    # 目的：驗證即使由 agent 回覆，也會補做監測資料入庫。
    # 為什麼：避免 LLM 已回覆完成語句但資料層未落地，造成回覆與紀錄不一致。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    db.add(
        RenalPatient(
            patient_code='P203',
            display_name='Agent補寫腎友',
            tel_no='0911777888',
            line_user_id='U_LINE_REPORT_004',
            enabled=True,
            is_diabetic=False,
            dry_weight_kg=63.0,
        )
    )
    db.commit()

    with (
        patch.object(line_integration, '_reply_line_message', new=AsyncMock(return_value=None)),
        patch.object(line_integration, '_run_agent_reply', return_value='您的血壓為114/74，體重為70.9公斤。'),
    ):
        response = client.post(
            '/api/line/webhook',
            json={
                'events': [
                    {
                        'type': 'message',
                        'source': {'type': 'user', 'userId': 'U_LINE_REPORT_004'},
                        'replyToken': 'reply-report-004',
                        'message': {'id': 'report-004', 'type': 'text', 'text': '請幫我記錄今天數值'},
                    }
                ]
            },
        )

    assert response.status_code == 200

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    messages_response = client.get(
        f"/api/line/sessions/{session.get('id')}/messages?limit=20",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    messages = messages_response.json().get('messages') or []
    assert any(message.get('sender_type') == 'agent' for message in messages)

    saved_records = db.query(MonitoringRecord).all()
    assert len(saved_records) == 1
    saved_measurements = saved_records[0].measurements or {}
    assert int(saved_measurements.get('systolic') or 0) == 114
    assert int(saved_measurements.get('diastolic') or 0) == 74
    assert float(saved_measurements.get('weight_kg') or 0) == 70.9


def test_line_webhook_duplicate_name_phone_moves_to_manual_review(client, admin_token, agent, db, monkeypatch):
    # 目的：驗證姓名與手機命中多筆時轉人工覆核。
    # 為什麼：同識別資料非唯一時若自動綁定，會有綁錯病患風險。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    db.add(RenalPatient(patient_code='P101', display_name='王小美', tel_no='0922333444', line_user_id=None, enabled=True))
    db.add(RenalPatient(patient_code='P102', display_name='王小美', tel_no='0922333444', line_user_id=None, enabled=True))
    db.commit()

    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_BIND_002'},
                    'message': {'id': 'dup-001', 'type': 'text', 'text': '嗨'},
                }
            ]
        },
    )
    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_BIND_002'},
                    'message': {'id': 'dup-002', 'type': 'text', 'text': '王小美'},
                }
            ]
        },
    )
    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_BIND_002'},
                    'message': {'id': 'dup-003', 'type': 'text', 'text': '0922-333-444'},
                }
            ]
        },
    )

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    session = (sessions_response.json().get('sessions') or [])[0]
    assert session.get('binding_status') == 'manual_review'


def test_line_operator_can_switch_human_mode_and_push_message(client, admin_token, agent, monkeypatch):
    # 目的：驗證操作員可切換 human 模式並主動推送訊息給 LINE 使用者。
    # 為什麼：人工接手是 LINE 客服場景關鍵，需確保 mode 與 push API 可用。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

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


def test_line_sessions_forbidden_without_line_permissions(client, regular_user_token, agent, monkeypatch):
    # 目的：驗證未具 LINE 中心/腎友照護權限的使用者不可讀取 LINE 會話。
    # 為什麼：LINE 訊息含敏感資料，不應由一般 chat 使用者檢視。
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_WEBHOOK_VERIFY_SIGNATURE', False)
    monkeypatch.setattr('src.api.routes.line_integration.settings.LINE_CHANNEL_ACCESS_TOKEN', 'line-test-token')
    monkeypatch.setattr(line_integration.line_onboarding_service, 'should_skip_event', AsyncMock(return_value=False))

    client.post(
        '/api/line/webhook',
        json={
            'events': [
                {
                    'type': 'message',
                    'source': {'type': 'user', 'userId': 'U_LINE_PERMISSION_001'},
                    'message': {'id': 'msg-perm-001', 'type': 'text', 'text': '你好'},
                }
            ]
        },
    )

    sessions_response = client.get('/api/line/sessions?limit=20', headers={'Authorization': f'Bearer {regular_user_token}'})
    assert sessions_response.status_code == 403
