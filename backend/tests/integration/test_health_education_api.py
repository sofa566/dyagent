from __future__ import annotations

from unittest.mock import AsyncMock, patch

from src.api.routes import scheduler_tasks
from src.core.config import settings
from src.models import Conversation, LineChannelSession
from src.models.renal_care import (
    HealthEducationContent,
    HealthEducationDeliveryLog,
    HealthEducationSourceRule,
    RenalPatient,
    ScheduledTask,
    ScheduledTaskRun,
    ScheduledTaskTemplate,
)


def _ensure_health_education_tables(db):
    RenalPatient.__table__.create(bind=db.bind, checkfirst=True)
    ScheduledTaskTemplate.__table__.create(bind=db.bind, checkfirst=True)
    ScheduledTask.__table__.create(bind=db.bind, checkfirst=True)
    ScheduledTaskRun.__table__.create(bind=db.bind, checkfirst=True)
    HealthEducationContent.__table__.create(bind=db.bind, checkfirst=True)
    HealthEducationDeliveryLog.__table__.create(bind=db.bind, checkfirst=True)
    HealthEducationSourceRule.__table__.create(bind=db.bind, checkfirst=True)


def _create_patient(db, *, patient_code: str, name: str, line_user_id: str | None, is_diabetic: bool) -> None:
    db.add(
        RenalPatient(
            patient_code=patient_code,
            display_name=name,
            line_user_id=line_user_id,
            is_diabetic=is_diabetic,
            enabled=True,
        )
    )
    db.commit()


def test_health_education_content_create_list_approve(client, db, admin_token):
    # 目的：驗證衛教內容可建立、查詢與核准。
    # 為什麼：MVP 需先完成內容治理，再進入發送流程。
    _ensure_health_education_tables(db)

    create_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '透析飲食重點',
            'source_name': 'udnhealth-hd',
            'source_url': 'https://udnhealth-hd.com/HD',
            'summary': '控制鉀離子原則',
            'tags': ['飲食'],
        },
    )
    assert create_response.status_code == 200
    content_id = create_response.json().get('item', {}).get('id')
    assert content_id

    list_response = client.get('/api/health-education/contents?status=draft&limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    assert list_response.status_code == 200
    assert any(item.get('id') == content_id for item in (list_response.json().get('items') or []))

    approve_response = client.put(f'/api/health-education/contents/{content_id}/approve', headers={'Authorization': f'Bearer {admin_token}'})
    assert approve_response.status_code == 200
    assert approve_response.json().get('item', {}).get('status') == 'approved'


def test_health_education_send_now_requires_approved(client, db, admin_token):
    # 目的：驗證未核准內容不可發送。
    # 為什麼：發送閘門需由後端硬限制，避免作業誤觸。
    _ensure_health_education_tables(db)

    create_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '洗腎前補水提醒',
            'source_name': 'health-site',
            'source_url': 'https://example.com/renal-water',
            'summary': '補水原則整理',
            'tags': ['補水'],
        },
    )
    content_id = create_response.json().get('item', {}).get('id')
    assert content_id

    send_response = client.post(
        f'/api/health-education/contents/{content_id}/send-now',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={'audience_rule': 'all'},
    )
    assert send_response.status_code == 400
    assert '尚未核准' in str(send_response.json().get('detail') or '')


def test_health_education_send_now_and_logs(client, db, admin_token):
    # 目的：驗證核准內容可發送並留下 sent/skipped 紀錄。
    # 為什麼：營運需要可追溯每篇內容的投遞成果。
    _ensure_health_education_tables(db)
    _create_patient(db, patient_code='HP001', name='王美華', line_user_id='U_HP001', is_diabetic=True)
    _create_patient(db, patient_code='HP002', name='陳小明', line_user_id=None, is_diabetic=False)
    bound_patient_row = db.query(RenalPatient).filter(RenalPatient.patient_code == 'HP001').first()
    assert bound_patient_row is not None
    bound_patient_row.tel_no = '0900000001'
    db.add(bound_patient_row)
    db.commit()

    create_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '高鉀飲食注意事項',
            'source_name': 'udnhealth-hd',
            'source_url': 'https://example.com/renal-potassium',
            'summary': '高鉀食物辨識重點',
            'tags': ['飲食', '鉀離子'],
        },
    )
    content_id = create_response.json().get('item', {}).get('id')
    approve_response = client.put(f'/api/health-education/contents/{content_id}/approve', headers={'Authorization': f'Bearer {admin_token}'})
    assert approve_response.status_code == 200

    with patch('src.services.health_education_service.health_education_service._push_line_message', new=AsyncMock(return_value=None)):
        send_response = client.post(
            f'/api/health-education/contents/{content_id}/send-now',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'audience_rule': 'all'},
        )

    assert send_response.status_code == 200
    assert int(send_response.json().get('sent') or 0) == 1
    assert int(send_response.json().get('skipped') or 0) == 1

    logs_response = client.get(
        f'/api/health-education/contents/{content_id}/logs?limit=20',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert logs_response.status_code == 200
    logs = logs_response.json().get('logs') or []
    assert len(logs) == 2
    assert any(str(item.get('status') or '') == 'sent' for item in logs)
    assert any(str(item.get('status') or '') == 'skipped' for item in logs)


def test_health_education_send_now_uses_active_bound_session_when_patient_line_user_missing(client, db, admin_token, agent):
    # 目的：驗證病患主檔缺 line_user_id 時，仍可用 active+bound session 發送衛教。
    # 為什麼：LINE 綁定可能先完成 session，主檔回填延遲，不能因此誤判 skipped。
    _ensure_health_education_tables(db)
    _create_patient(db, patient_code='HP003', name='李老頭', line_user_id=None, is_diabetic=False)
    patient_row = db.query(RenalPatient).filter(RenalPatient.patient_code == 'HP003').first()
    assert patient_row is not None
    patient_row.tel_no = None
    db.add(patient_row)
    db.flush()

    conversation_row = Conversation(agent_id=agent.id, title='health-education-dispatch-session')
    db.add(conversation_row)
    db.flush()
    db.add(
        LineChannelSession(
            line_user_id='U_HP003_SESSION',
            conversation_id=conversation_row.id,
            assigned_agent_id=agent.id,
            bound_patient_id=patient_row.id,
            binding_status='bound',
            mode='bot',
            status='active',
        )
    )
    db.commit()

    create_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '慢性腎臟病飲食原則',
            'source_name': 'health-site',
            'source_url': 'https://example.com/renal-diet-rule',
            'summary': '衛教摘要',
            'tags': ['飲食'],
        },
    )
    content_id = create_response.json().get('item', {}).get('id')
    assert content_id
    approve_response = client.put(f'/api/health-education/contents/{content_id}/approve', headers={'Authorization': f'Bearer {admin_token}'})
    assert approve_response.status_code == 200

    with patch('src.services.health_education_service.health_education_service._push_line_message', new=AsyncMock(return_value=None)) as push_mock:
        send_response = client.post(
            f'/api/health-education/contents/{content_id}/send-now',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'audience_rule': 'all'},
        )

    assert send_response.status_code == 200
    send_payload = send_response.json()
    assert int(send_payload.get('sent') or 0) == 1
    assert int(send_payload.get('skipped') or 0) == 0
    assert push_mock.await_count == 1
    assert push_mock.await_args.kwargs.get('to_line_user_id') == 'U_HP003_SESSION'


def test_health_education_schedule_creates_scheduler_task(client, db, admin_token):
    # 目的：驗證核准內容可建立排程任務。
    # 為什麼：MVP 需支援定時發送而非僅手動推播。
    _ensure_health_education_tables(db)

    create_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '透析後照護提醒',
            'source_name': 'health-site',
            'source_url': 'https://example.com/renal-after-care',
            'summary': '透析後注意事項',
            'tags': ['透析'],
        },
    )
    content_id = create_response.json().get('item', {}).get('id')
    client.put(f'/api/health-education/contents/{content_id}/approve', headers={'Authorization': f'Bearer {admin_token}'})

    schedule_response = client.post(
        f'/api/health-education/contents/{content_id}/schedule',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '衛教-每週三晚間',
            'cron_expression': '0 20 * * 3',
            'timezone': 'Asia/Taipei',
            'audience_rule': 'all',
            'enabled': True,
        },
    )
    assert schedule_response.status_code == 200
    task = schedule_response.json().get('task') or {}
    assert str(task.get('task_type') or '') == 'health_education_dispatch'
    payload = task.get('payload') or {}
    assert str(payload.get('content_id') or '') == str(content_id)


def test_health_education_scheduler_rotate_mode_respects_cooldown(client, db, admin_token):
    # 目的：驗證衛教排程可用 rotate 模式自動換文。
    # 為什麼：固定排程需避免短期重複發送同一篇文章，符合營運月內不重複需求。
    _ensure_health_education_tables(db)
    _create_patient(db, patient_code='HP003', name='李阿秀', line_user_id='U_HP003', is_diabetic=False)

    first_content_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '飲食控制原則 A',
            'source_name': 'health-site',
            'source_url': 'https://example.com/education-rotate-a',
            'summary': '第一篇內容',
            'tags': ['飲食'],
        },
    )
    second_content_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '飲食控制原則 B',
            'source_name': 'health-site',
            'source_url': 'https://example.com/education-rotate-b',
            'summary': '第二篇內容',
            'tags': ['飲食'],
        },
    )
    first_content_id = str(first_content_response.json().get('item', {}).get('id') or '')
    second_content_id = str(second_content_response.json().get('item', {}).get('id') or '')
    assert first_content_id and second_content_id

    client.put(f'/api/health-education/contents/{first_content_id}/approve', headers={'Authorization': f'Bearer {admin_token}'})
    client.put(f'/api/health-education/contents/{second_content_id}/approve', headers={'Authorization': f'Bearer {admin_token}'})

    with patch('src.services.health_education_service.health_education_service._push_line_message', new=AsyncMock(return_value=None)):
        warmup_send_response = client.post(
            f'/api/health-education/contents/{first_content_id}/send-now',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'audience_rule': 'all'},
        )
    assert warmup_send_response.status_code == 200

    template_response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': 'test.template.health.rotate',
            'name': '衛教輪替模板',
            'description': '測試衛教輪替任務',
            'executor_type': 'health_education_dispatch',
            'payload_schema': {},
            'default_payload': {},
            'enabled': True,
        },
    )
    assert template_response.status_code == 200
    template_id = str(template_response.json().get('item', {}).get('id') or '')
    assert template_id

    create_task_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '衛教輪替發送',
            'description': '測試 30 天冷卻',
            'cron_expression': '0 9 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_id,
            'payload': {'dispatch_mode': 'rotate', 'audience_rule': 'all', 'cooldown_days': 30},
            'enabled': True,
        },
    )
    assert create_task_response.status_code == 200
    task_id = str(create_task_response.json().get('item', {}).get('id') or '')
    assert task_id

    with (
        patch.object(scheduler_tasks.scheduler_task_service, '_dispatch_run_to_celery', return_value=False),
        patch('src.services.health_education_service.health_education_service._push_line_message', new=AsyncMock(return_value=None)),
    ):
        run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})
    assert run_response.status_code == 200
    run_payload = run_response.json().get('run') or {}
    output_payload = run_payload.get('output_payload') or {}
    assert run_payload.get('status') == 'success'
    assert str(output_payload.get('dispatch_mode') or '') == 'rotate'
    assert str(output_payload.get('selected_content_id') or '') == second_content_id
    assert str(output_payload.get('content_id') or '') == second_content_id


def test_health_education_scheduler_rotate_mode_emits_empty_pool_alert(client, db, admin_token):
    # 目的：驗證衛教輪替任務連續無可發文章時會回傳告警。
    # 為什麼：營運需要及早發現內容池不足，避免排程持續空轉。
    _ensure_health_education_tables(db)
    _create_patient(db, patient_code='HP004', name='周小芬', line_user_id='U_HP004', is_diabetic=False)
    patient_row = db.query(RenalPatient).filter(RenalPatient.patient_code == 'HP004').first()
    assert patient_row is not None
    patient_row.tel_no = '0900000004'
    db.add(patient_row)
    db.commit()

    create_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '運動衛教 C',
            'source_name': 'health-site',
            'source_url': 'https://example.com/education-rotate-c',
            'summary': '唯一候選文章',
            'tags': ['運動'],
        },
    )
    content_id = str(create_response.json().get('item', {}).get('id') or '')
    assert content_id
    client.put(f'/api/health-education/contents/{content_id}/approve', headers={'Authorization': f'Bearer {admin_token}'})

    with patch('src.services.health_education_service.health_education_service._push_line_message', new=AsyncMock(return_value=None)):
        warmup_send_response = client.post(
            f'/api/health-education/contents/{content_id}/send-now',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'audience_rule': 'all'},
        )
    assert warmup_send_response.status_code == 200
    assert int(warmup_send_response.json().get('sent') or 0) == 1

    template_response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': 'test.template.health.rotate.alert',
            'name': '衛教輪替告警模板',
            'description': '測試輪替空池告警',
            'executor_type': 'health_education_dispatch',
            'payload_schema': {},
            'default_payload': {},
            'enabled': True,
        },
    )
    assert template_response.status_code == 200
    template_id = str(template_response.json().get('item', {}).get('id') or '')
    assert template_id

    create_task_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '衛教輪替空池告警',
            'description': '測試連續無可發文章告警',
            'cron_expression': '0 10 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_id,
            'payload': {'dispatch_mode': 'rotate', 'audience_rule': 'all', 'cooldown_days': 30},
            'enabled': True,
        },
    )
    assert create_task_response.status_code == 200
    task_id = str(create_task_response.json().get('item', {}).get('id') or '')
    assert task_id

    with patch.object(scheduler_tasks.scheduler_task_service, '_dispatch_run_to_celery', return_value=False):
        first_run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})
        second_run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})
        third_run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})
    assert first_run_response.status_code == 200
    assert second_run_response.status_code == 200
    assert third_run_response.status_code == 200

    runs_response = client.get(f'/api/scheduler/tasks/{task_id}/runs?limit=5', headers={'Authorization': f'Bearer {admin_token}'})
    assert runs_response.status_code == 200
    alert_rows = runs_response.json().get('alerts') or []
    assert len(alert_rows) == 1
    assert str(alert_rows[0].get('code') or '') == 'health_education_rotate_no_eligible_content'
    assert int(alert_rows[0].get('consecutive_runs') or 0) >= 3


def test_health_education_delete_single_content(client, db, admin_token):
    # 目的：驗證可刪除單筆衛教內容。
    # 為什麼：營運需可移除誤匯入資料，保持列表整潔。
    _ensure_health_education_tables(db)

    create_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '待刪除內容',
            'source_name': 'health-site',
            'source_url': 'https://delete.example.org/item-1',
            'summary': '刪除測試',
            'tags': ['測試'],
        },
    )
    content_id = create_response.json().get('item', {}).get('id')
    assert content_id

    delete_response = client.delete(
        f'/api/health-education/contents/{content_id}',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert delete_response.status_code == 200
    assert str(delete_response.json().get('deleted_content_id') or '') == str(content_id)

    list_response = client.get('/api/health-education/contents?status=draft&limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    assert list_response.status_code == 200
    assert all(str(item.get('id') or '') != str(content_id) for item in (list_response.json().get('items') or []))


def test_health_education_clear_rejected_contents(client, db, admin_token):
    # 目的：驗證可批次清除 rejected 內容。
    # 為什麼：大量退回資料需快速清理，不應逐筆刪除。
    _ensure_health_education_tables(db)

    first_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '退回內容A',
            'source_name': 'health-site',
            'source_url': 'https://delete.example.org/rejected-a',
            'summary': '退回 A',
            'tags': ['測試'],
        },
    )
    second_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '退回內容B',
            'source_name': 'health-site',
            'source_url': 'https://delete.example.org/rejected-b',
            'summary': '退回 B',
            'tags': ['測試'],
        },
    )
    content_id_a = first_response.json().get('item', {}).get('id')
    content_id_b = second_response.json().get('item', {}).get('id')
    assert content_id_a and content_id_b

    client.put(f'/api/health-education/contents/{content_id_a}/reject', headers={'Authorization': f'Bearer {admin_token}'})
    client.put(f'/api/health-education/contents/{content_id_b}/reject', headers={'Authorization': f'Bearer {admin_token}'})

    clear_response = client.delete('/api/health-education/contents/rejected', headers={'Authorization': f'Bearer {admin_token}'})
    assert clear_response.status_code == 200
    assert int(clear_response.json().get('deleted_contents') or 0) == 2

    list_rejected_response = client.get('/api/health-education/contents?status=rejected&limit=20', headers={'Authorization': f'Bearer {admin_token}'})
    assert list_rejected_response.status_code == 200
    assert (list_rejected_response.json().get('items') or []) == []


def test_health_education_import_candidates_supports_agent_and_dedupe(client, db, admin_token):
    # 目的：驗證可批次匯入 Agent 候選內容並自動去重。
    # 為什麼：降低人工逐筆貼入負擔，讓搜尋結果快速進入審核流程。
    _ensure_health_education_tables(db)

    with patch('src.services.health_education_service.health_education_service._is_source_url_accessible', return_value=True):
        response = client.post(
            '/api/health-education/contents/import-candidates',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'agent_name': '腎友衛教搜尋',
                'items': [
                    {
                        'title': '透析飲食原則',
                        'source_name': '站點A',
                        'source_url': 'https://example.com/a',
                        'summary': '摘要A',
                        'tags': ['飲食'],
                    },
                    {
                        'title': '透析飲食原則-重複',
                        'source_name': '站點A',
                        'source_url': 'https://example.com/a',
                        'summary': '摘要A',
                        'tags': ['飲食'],
                    },
                ],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert int(payload.get('created') or 0) == 1
    assert int(payload.get('skipped_duplicate') or 0) == 1
    first_item = (payload.get('items') or [])[0]
    tags = first_item.get('tags') or []
    assert any(str(tag).startswith('agent:腎友衛教搜尋') for tag in tags)


def test_health_education_source_policy_blocks_domain(client, db, admin_token):
    # 目的：驗證可擴充封鎖網域規則會阻止候選內容新增。
    # 為什麼：來源策略需可治理，避免已知不可信來源進入審核池。
    _ensure_health_education_tables(db)

    original_blocked = str(getattr(settings, 'HEALTH_EDUCATION_BLOCKED_DOMAINS', '') or '')
    settings.HEALTH_EDUCATION_BLOCKED_DOMAINS = 'blocked.example.com'
    try:
        policy_response = client.get('/api/health-education/source-policy', headers={'Authorization': f'Bearer {admin_token}'})
        assert policy_response.status_code == 200
        assert 'blocked.example.com' in (policy_response.json().get('policy', {}).get('blocked_domains') or [])

        create_response = client.post(
            '/api/health-education/contents',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'title': '封鎖來源測試',
                'source_name': 'blocked',
                'source_url': 'https://blocked.example.com/post',
                'summary': '不應建立',
                'tags': ['測試'],
            },
        )
        assert create_response.status_code == 400
        assert '封鎖' in str(create_response.json().get('detail') or '')
    finally:
        settings.HEALTH_EDUCATION_BLOCKED_DOMAINS = original_blocked


def test_health_education_block_rule_with_www_matches_root_domain(client, db, admin_token):
    # 目的：驗證黑名單規則使用 www 網域時，仍可攔截同根網域來源。
    # 為什麼：營運常混用 `www.domain` 與 `domain`，規則應保持一致。
    _ensure_health_education_tables(db)

    create_rule_response = client.post(
        '/api/health-education/source-rules',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'domain': 'www.kidney.org',
            'policy': 'blocked',
            'enabled': True,
            'note': '測試 www 比對',
        },
    )
    assert create_rule_response.status_code == 200

    create_content_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '黑名單比對',
            'source_name': 'Kidney',
            'source_url': 'https://kidney.org/article',
            'summary': '不應建立',
            'tags': ['測試'],
        },
    )
    assert create_content_response.status_code == 400
    assert '封鎖' in str(create_content_response.json().get('detail') or '')


def test_health_education_source_rules_crud(client, db, admin_token):
    # 目的：驗證可擴充白名單/黑名單規則可透過 API 維護。
    # 為什麼：營運需即時增刪來源規則，不能依賴環境變數部署流程。
    _ensure_health_education_tables(db)

    create_response = client.post(
        '/api/health-education/source-rules',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'domain': 'Health.Example.org',
            'policy': 'trusted',
            'enabled': True,
            'note': '人工新增',
        },
    )
    assert create_response.status_code == 200
    created_rule = create_response.json().get('item') or {}
    rule_id = str(created_rule.get('id') or '')
    assert rule_id
    assert str(created_rule.get('domain') or '') == 'health.example.org'

    list_response = client.get('/api/health-education/source-rules', headers={'Authorization': f'Bearer {admin_token}'})
    assert list_response.status_code == 200
    assert any(str(item.get('id') or '') == rule_id for item in (list_response.json().get('items') or []))

    update_response = client.put(
        f'/api/health-education/source-rules/{rule_id}',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'domain': 'health-blocked.example.org',
            'policy': 'blocked',
            'enabled': True,
            'note': '改為封鎖',
        },
    )
    assert update_response.status_code == 200
    updated_rule = update_response.json().get('item') or {}
    assert str(updated_rule.get('policy') or '') == 'blocked'

    policy_response = client.get('/api/health-education/source-policy', headers={'Authorization': f'Bearer {admin_token}'})
    assert policy_response.status_code == 200
    blocked_domains = policy_response.json().get('policy', {}).get('blocked_domains') or []
    assert 'health-blocked.example.org' in blocked_domains

    delete_response = client.delete(f'/api/health-education/source-rules/{rule_id}', headers={'Authorization': f'Bearer {admin_token}'})
    assert delete_response.status_code == 200


def test_health_education_source_rules_list_orders_by_created_desc(client, db, admin_token):
    # 目的：驗證來源規則列表以建立時間倒序。
    # 為什麼：更新單筆規則後不應讓列表位置跳動，便於營運維護。
    _ensure_health_education_tables(db)

    first_response = client.post(
        '/api/health-education/source-rules',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'domain': 'order-first.example.org',
            'policy': 'trusted',
            'enabled': True,
            'note': '第一筆',
        },
    )
    assert first_response.status_code == 200
    first_rule_id = str(first_response.json().get('item', {}).get('id') or '')
    assert first_rule_id

    second_response = client.post(
        '/api/health-education/source-rules',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'domain': 'order-second.example.org',
            'policy': 'trusted',
            'enabled': True,
            'note': '第二筆',
        },
    )
    assert second_response.status_code == 200
    second_rule_id = str(second_response.json().get('item', {}).get('id') or '')
    assert second_rule_id

    list_response = client.get('/api/health-education/source-rules', headers={'Authorization': f'Bearer {admin_token}'})
    assert list_response.status_code == 200
    items = list_response.json().get('items') or []
    assert len(items) >= 2
    assert str(items[0].get('id') or '') == second_rule_id
    assert str(items[1].get('id') or '') == first_rule_id


def test_health_education_source_rule_url_prefix_only_blocks_target_path(client, db, admin_token):
    # 目的：驗證來源規則可使用網址前綴（含路徑）做細粒度控管。
    # 為什麼：同網域多語系網站需僅封鎖指定語系路徑，不能一刀切整個網域。
    _ensure_health_education_tables(db)

    create_rule_response = client.post(
        '/api/health-education/source-rules',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'domain': 'https://kidneyeducation.com/Chinese/',
            'policy': 'blocked',
            'enabled': True,
            'note': '封鎖中文路徑',
        },
    )
    assert create_rule_response.status_code == 200
    created_rule = create_rule_response.json().get('item') or {}
    assert str(created_rule.get('domain') or '') == 'https://kidneyeducation.com/Chinese'

    blocked_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '中文頁面應被封鎖',
            'source_name': 'kidneyeducation',
            'source_url': 'https://kidneyeducation.com/Chinese/article-1',
            'summary': '測試路徑封鎖',
            'tags': ['測試'],
        },
    )
    assert blocked_response.status_code == 400
    assert '封鎖' in str(blocked_response.json().get('detail') or '')

    allowed_response = client.post(
        '/api/health-education/contents',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'title': '英文頁面可進審核池',
            'source_name': 'kidneyeducation',
            'source_url': 'https://kidneyeducation.com/English/article-2',
            'summary': '測試路徑差異',
            'tags': ['測試'],
        },
    )
    assert allowed_response.status_code == 200


def test_health_education_source_rule_scheme_less_url_prefix_keeps_path(client, db, admin_token):
    # 目的：驗證未帶 scheme 的網址前綴輸入仍保留路徑。
    # 為什麼：營運常直接輸入 domain/path，系統不應默默退化成僅網域造成編輯看似失效。
    _ensure_health_education_tables(db)

    create_response = client.post(
        '/api/health-education/source-rules',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'domain': 'kidneyeducation.com/Chinese/',
            'policy': 'trusted',
            'enabled': True,
            'note': '不帶 scheme 測試',
        },
    )
    assert create_response.status_code == 200
    created_item = create_response.json().get('item') or {}
    assert str(created_item.get('domain') or '') == 'https://kidneyeducation.com/Chinese'


def test_health_education_import_candidates_skips_unreachable(client, db, admin_token):
    # 目的：驗證候選來源連結不可存取時會被跳過。
    # 為什麼：避免匯入後使用者點擊連結卻無法開啟。
    _ensure_health_education_tables(db)

    with patch('src.services.health_education_service.health_education_service._is_source_url_accessible', return_value=False):
        response = client.post(
            '/api/health-education/contents/import-candidates',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'agent_name': '腎友衛教搜尋',
                'items': [
                    {
                        'title': '不可達內容',
                        'source_name': '站點X',
                        'source_url': 'https://example.com/unreachable',
                        'summary': '摘要',
                        'tags': ['測試'],
                    },
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert int(payload.get('created') or 0) == 0
    assert int(payload.get('skipped_unreachable') or 0) == 1
    skipped_items = payload.get('skipped_items') or []
    assert isinstance(skipped_items, list)
    assert skipped_items and str(skipped_items[0].get('reason') or '') == 'unreachable'


def test_health_education_import_candidates_trusted_only_mode(client, db, admin_token):
    # 目的：驗證白名單強制模式下，非 trusted 來源會被略過。
    # 為什麼：營運流程需可一鍵限制來源品質，避免登入牆或不穩定站點混入。
    _ensure_health_education_tables(db)

    original_trusted = str(getattr(settings, 'HEALTH_EDUCATION_TRUSTED_DOMAINS', '') or '')
    original_trusted_only = bool(getattr(settings, 'HEALTH_EDUCATION_TRUSTED_ONLY_MODE', False))
    settings.HEALTH_EDUCATION_TRUSTED_DOMAINS = 'trusted.example.com'
    settings.HEALTH_EDUCATION_TRUSTED_ONLY_MODE = True
    try:
        with patch('src.services.health_education_service.health_education_service._is_source_url_accessible', return_value=True):
            response = client.post(
                '/api/health-education/contents/import-candidates',
                headers={'Authorization': f'Bearer {admin_token}'},
                json={
                    'agent_name': '腎友衛教搜尋',
                    'items': [
                        {
                            'title': '白名單來源',
                            'source_name': 'Trusted',
                            'source_url': 'https://trusted.example.com/a',
                            'summary': '摘要A',
                            'tags': ['測試'],
                        },
                        {
                            'title': '非白名單來源',
                            'source_name': 'Other',
                            'source_url': 'https://other.example.com/b',
                            'summary': '摘要B',
                            'tags': ['測試'],
                        },
                    ],
                },
            )
    finally:
        settings.HEALTH_EDUCATION_TRUSTED_DOMAINS = original_trusted
        settings.HEALTH_EDUCATION_TRUSTED_ONLY_MODE = original_trusted_only

    assert response.status_code == 200
    payload = response.json()
    assert int(payload.get('created') or 0) == 1
    assert int(payload.get('skipped_not_trusted') or 0) == 1
    skipped_items = payload.get('skipped_items') or []
    assert any(str(item.get('reason') or '') == 'not_trusted' for item in skipped_items)


def test_health_education_import_from_mcp_creates_candidates(client, db, admin_token):
    # 目的：驗證可不依賴 Agent 直接透過 MCP 搜尋並匯入候選內容。
    # 為什麼：衛教匯入主流程已改為 MCP-first，應可在無 Agent 前提下正常運作。
    _ensure_health_education_tables(db)

    fake_mcp_response = {
        'ok': True,
        'result': {
            'items': [
                {
                    'title': '透析飲食重點',
                    'url': 'https://www.tsn.org.tw/education/dialysis-diet',
                    'description': '透析患者飲食建議。',
                }
            ]
        },
    }

    with patch('src.services.health_education_service.ChatRouter.call_mcp_tool', return_value=fake_mcp_response):
        with patch('src.services.health_education_service.health_education_service._is_source_url_accessible', return_value=True):
            response = client.post(
                '/api/health-education/contents/import-from-mcp',
                headers={'Authorization': f'Bearer {admin_token}'},
                json={'topic': '透析飲食', 'limit': 3},
            )

    assert response.status_code == 200
    payload = response.json()
    assert int(payload.get('created') or 0) == 1
    assert int(payload.get('parsed_items') or 0) == 1
    assert 'mcp_raw_output_preview' in payload
    assert payload.get('mcp_raw_output_preview') == payload.get('agent_raw_output_preview')
    mcp_search = payload.get('mcp_search') or {}
    assert bool(mcp_search.get('call_ok')) is True


def test_health_education_import_from_mcp_parses_content_text_payload(client, db, admin_token):
    # 目的：驗證可從 MCP content.text 包裹 JSON 的格式萃取候選內容。
    # 為什麼：firecrawl_search 常將結果包在文字欄位，流程需能正確解包。
    _ensure_health_education_tables(db)

    wrapped_result = {
        'ok': True,
        'result': {
            'content': [
                {
                    'type': 'text',
                    'text': '{"success":true,"data":{"web":[{"url":"https://www.tsn.org.tw/education/dialysis-diet","title":"透析飲食","description":"透析患者飲食重點"}]}}',
                }
            ]
        },
    }

    with patch('src.services.health_education_service.ChatRouter.call_mcp_tool', return_value=wrapped_result):
        with patch('src.services.health_education_service.health_education_service._is_source_url_accessible', return_value=True):
            response = client.post(
                '/api/health-education/contents/import-from-mcp',
                headers={'Authorization': f'Bearer {admin_token}'},
                json={'topic': '透析飲食', 'limit': 3},
            )

    assert response.status_code == 200
    payload = response.json()
    assert int(payload.get('created') or 0) == 1
    assert int(payload.get('parsed_items') or 0) == 1


def test_health_education_import_from_mcp_returns_readable_error_when_empty(client, db, admin_token):
    # 目的：驗證 MCP 呼叫成功但無可匯入內容時，會回覆可讀錯誤訊息。
    # 為什麼：營運需在畫面直接判斷是搜尋無結果，而非系統例外。
    _ensure_health_education_tables(db)

    with patch('src.services.health_education_service.ChatRouter.call_mcp_tool', return_value={'ok': True, 'result': {'items': []}}):
        response = client.post(
            '/api/health-education/contents/import-from-mcp',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'topic': '透析照護', 'limit': 5},
        )

    assert response.status_code == 200
    payload = response.json()
    assert int(payload.get('created') or 0) == 0
    assert 'mcp_error' in payload
    assert 'MCP 搜尋' in str(payload.get('mcp_error') or '')
    assert 'agent_error' in payload
    assert 'MCP 搜尋' in str(payload.get('agent_error') or '')
