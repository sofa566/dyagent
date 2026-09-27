from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.api.routes import scheduler_tasks
from src.models.renal_care import ScheduledTask, ScheduledTaskRun, ScheduledTaskTemplate


def _ensure_scheduler_tables(db):
    ScheduledTaskTemplate.__table__.create(bind=db.bind, checkfirst=True)
    ScheduledTask.__table__.create(bind=db.bind, checkfirst=True)
    ScheduledTaskRun.__table__.create(bind=db.bind, checkfirst=True)


def _create_template(client, admin_token, executor_type: str = 'monitoring_backfill') -> dict:
    response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': f'test.template.{executor_type}',
            'name': f'測試模板-{executor_type}',
            'description': '測試用途',
            'executor_type': executor_type,
            'payload_schema': {'required': ['lookback_days']} if executor_type == 'monitoring_backfill' else {},
            'default_payload': {'lookback_days': 1} if executor_type == 'monitoring_backfill' else {},
            'enabled': True,
        },
    )
    assert response.status_code == 200
    return response.json().get('item') or {}


def test_scheduler_template_crud(client, admin_token, db):
    # 目的：驗證模板可建立、更新、查詢、刪除。
    # 為什麼：任務需綁定模板才可統一 executor 與 payload schema。
    _ensure_scheduler_tables(db)

    create_response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': 'test.template.http_call',
            'name': '測試-HTTP 模板',
            'description': '測試建立',
            'executor_type': 'http_call',
            'payload_schema': {'required': ['url']},
            'default_payload': {'method': 'GET'},
            'enabled': True,
        },
    )
    assert create_response.status_code == 200
    template_id = create_response.json().get('item', {}).get('id')
    assert template_id

    list_response = client.get('/api/scheduler/templates?enabled_only=false', headers={'Authorization': f'Bearer {admin_token}'})
    assert list_response.status_code == 200
    assert any(row.get('id') == template_id for row in (list_response.json().get('items') or []))

    update_response = client.put(
        f'/api/scheduler/templates/{template_id}',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={'name': '測試-HTTP 模板-更新', 'enabled': True},
    )
    assert update_response.status_code == 200
    assert update_response.json().get('item', {}).get('name') == '測試-HTTP 模板-更新'

    delete_response = client.delete(f'/api/scheduler/templates/{template_id}', headers={'Authorization': f'Bearer {admin_token}'})
    assert delete_response.status_code == 200


def test_scheduler_task_crud_and_run_now(client, admin_token, db):
    # 目的：驗證可建立、更新、查詢、刪除 Crontab 任務。
    # 為什麼：後台排程管理頁需完整 CRUD 才能滿足 schedule 管理需求。
    _ensure_scheduler_tables(db)
    template_item = _create_template(client, admin_token, executor_type='monitoring_backfill')

    create_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '測試-補寫任務',
            'description': '測試建立',
            'cron_expression': '0 6 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_item.get('id'),
            'payload': {'lookback_days': 2, 'max_scan_rows': 50},
            'enabled': True,
        },
    )
    assert create_response.status_code == 200
    task_item = create_response.json().get('item') or {}
    task_id = task_item.get('id')
    assert task_id
    assert task_item.get('task_type') == 'monitoring_backfill'

    list_response = client.get('/api/scheduler/tasks?enabled_only=false', headers={'Authorization': f'Bearer {admin_token}'})
    assert list_response.status_code == 200
    assert any(row.get('id') == task_id for row in (list_response.json().get('items') or []))

    update_response = client.put(
        f'/api/scheduler/tasks/{task_id}',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={'cron_expression': '5 6 * * *', 'description': '已更新', 'enabled': True},
    )
    assert update_response.status_code == 200
    assert update_response.json().get('item', {}).get('cron_expression') == '5 6 * * *'

    delete_response = client.delete(f'/api/scheduler/tasks/{task_id}', headers={'Authorization': f'Bearer {admin_token}'})
    assert delete_response.status_code == 200
    final_list = client.get('/api/scheduler/tasks?enabled_only=false', headers={'Authorization': f'Bearer {admin_token}'})
    assert not any(row.get('id') == task_id for row in (final_list.json().get('items') or []))


def test_scheduler_task_run_now_enqueues_or_fallback_inline(client, admin_token, db):
    # 目的：驗證 Run Now 可建立 run log 並執行任務。
    # 為什麼：Celery 佇列不可用時仍需可回退執行，避免後台操作完全阻塞。
    _ensure_scheduler_tables(db)
    template_item = _create_template(client, admin_token, executor_type='monitoring_backfill')

    create_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '測試-手動執行',
            'description': 'run now 測試',
            'cron_expression': '10 12 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_item.get('id'),
            'payload': {'lookback_days': 1, 'max_scan_rows': 100},
            'enabled': True,
        },
    )
    assert create_response.status_code == 200
    task_id = create_response.json().get('item', {}).get('id')
    assert task_id
    assert db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first() is not None

    with (
        patch.object(scheduler_tasks.scheduler_task_service, '_dispatch_run_to_celery', return_value=False),
        patch.object(scheduler_tasks.scheduler_task_service, '_execute_monitoring_backfill', new=AsyncMock(return_value={'ok': True, 'created_records': 0})),
    ):
        run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})

    assert run_response.status_code == 200
    assert run_response.json().get('queued') is False
    assert run_response.json().get('run', {}).get('status') == 'success'

    runs_response = client.get(f'/api/scheduler/tasks/{task_id}/runs?limit=10', headers={'Authorization': f'Bearer {admin_token}'})
    assert runs_response.status_code == 200
    assert len(runs_response.json().get('runs') or []) >= 1

    delete_response = client.delete(f'/api/scheduler/tasks/{task_id}', headers={'Authorization': f'Bearer {admin_token}'})
    assert delete_response.status_code == 200


def test_scheduler_task_python_script_executor(client, admin_token, db):
    # 目的：驗證 python_script executor 可執行腳本並記錄結果。
    # 為什麼：使用者要求支援 python 腳本排程，需確保 Run Now 可運作。
    _ensure_scheduler_tables(db)
    template_response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': 'test.template.python',
            'name': '測試-Python 模板',
            'description': '執行 python 檔案',
            'executor_type': 'python_script',
            'payload_schema': {'required': ['file_path']},
            'default_payload': {},
            'enabled': True,
        },
    )
    assert template_response.status_code == 200
    template_id = template_response.json().get('item', {}).get('id')
    assert template_id

    script_path = Path(__file__).resolve().parent / '_tmp_scheduler_script.py'
    script_path.write_text('print("scheduler-ok")\n', encoding='utf-8')

    create_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '測試-python-script',
            'description': '執行 python script',
            'cron_expression': '0 9 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_id,
            'payload': {'file_path': str(script_path)},
            'enabled': True,
        },
    )
    assert create_response.status_code == 200
    task_id = create_response.json().get('item', {}).get('id')
    assert task_id

    try:
        with patch.object(scheduler_tasks.scheduler_task_service, '_dispatch_run_to_celery', return_value=False):
            run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})

        assert run_response.status_code == 200
        run_payload = run_response.json().get('run') or {}
        assert run_payload.get('status') == 'success'
        output_payload = run_payload.get('output_payload') or {}
        assert 'scheduler-ok' in str(output_payload.get('stdout_preview') or '')
    finally:
        if script_path.exists():
            script_path.unlink()


def test_scheduler_task_script_path_outside_allowed_roots_fails(client, admin_token, db):
    # 目的：驗證腳本路徑白名單會阻擋非授權目錄。
    # 為什麼：避免排程任務可直接讀取或執行任意系統檔案。
    _ensure_scheduler_tables(db)
    template_response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': 'test.template.python.blocked',
            'name': '測試-Python 模板-阻擋路徑',
            'description': '驗證白名單阻擋',
            'executor_type': 'python_script',
            'payload_schema': {'required': ['file_path']},
            'default_payload': {},
            'enabled': True,
        },
    )
    assert template_response.status_code == 200
    template_id = template_response.json().get('item', {}).get('id')
    assert template_id

    create_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '測試-python-script-blocked',
            'description': '路徑阻擋',
            'cron_expression': '0 9 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_id,
            'payload': {'file_path': '/etc/hosts'},
            'enabled': True,
        },
    )
    assert create_response.status_code == 200
    task_id = create_response.json().get('item', {}).get('id')
    assert task_id

    with patch.object(scheduler_tasks.scheduler_task_service, '_dispatch_run_to_celery', return_value=False):
        run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})

    assert run_response.status_code == 200
    run_payload = run_response.json().get('run') or {}
    assert run_payload.get('status') == 'failed'
    assert '路徑不在允許範圍內' in str(run_payload.get('error_message') or '')


def test_scheduler_task_bash_disallow_unsafe_operator(client, admin_token, db):
    # 目的：驗證 bash 指令會擋掉危險運算子。
    # 為什麼：避免用複合指令繞過任務執行邊界。
    _ensure_scheduler_tables(db)
    template_response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': 'test.template.bash.blocked',
            'name': '測試-Bash 模板-阻擋運算子',
            'description': '驗證 command 安全限制',
            'executor_type': 'bash_script',
            'payload_schema': {'required': ['command']},
            'default_payload': {},
            'enabled': True,
        },
    )
    assert template_response.status_code == 200
    template_id = template_response.json().get('item', {}).get('id')
    assert template_id

    create_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '測試-bash-unsafe',
            'description': 'bash 安全限制',
            'cron_expression': '0 9 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_id,
            'payload': {'command': 'python --version && whoami'},
            'enabled': True,
        },
    )
    assert create_response.status_code == 200
    task_id = create_response.json().get('item', {}).get('id')
    assert task_id

    with patch.object(scheduler_tasks.scheduler_task_service, '_dispatch_run_to_celery', return_value=False):
        run_response = client.post(f'/api/scheduler/tasks/{task_id}/run', headers={'Authorization': f'Bearer {admin_token}'})

    assert run_response.status_code == 200
    run_payload = run_response.json().get('run') or {}
    assert run_payload.get('status') == 'failed'
    assert '不允許的特殊運算子' in str(run_payload.get('error_message') or '')


def test_scheduler_task_health_education_rotate_allows_missing_content_id(client, admin_token, db):
    # 目的：驗證衛教 rotate 模式可不帶 content_id。
    # 為什麼：歷史模板可能仍要求 content_id，建立 rotate 任務時不應被舊 schema 阻擋。
    _ensure_scheduler_tables(db)
    template_response = client.post(
        '/api/scheduler/templates',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'template_key': 'test.template.health.rotate.no-content-id',
            'name': '測試-衛教輪替',
            'description': 'rotate 任務不需 content_id',
            'executor_type': 'health_education_dispatch',
            'payload_schema': {'required': ['content_id']},
            'default_payload': {'audience_rule': 'all'},
            'enabled': True,
        },
    )
    assert template_response.status_code == 200
    template_id = template_response.json().get('item', {}).get('id')
    assert template_id

    create_task_response = client.post(
        '/api/scheduler/tasks',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '測試-衛教輪替任務',
            'description': 'rotate 不填 content_id',
            'cron_expression': '0 8 * * *',
            'timezone': 'Asia/Taipei',
            'template_id': template_id,
            'payload': {'dispatch_mode': 'rotate', 'audience_rule': 'all', 'cooldown_days': 30},
            'enabled': True,
        },
    )
    assert create_task_response.status_code == 200
    task_item = create_task_response.json().get('item') or {}
    payload = task_item.get('payload') or {}
    assert str(payload.get('dispatch_mode') or '') == 'rotate'
    assert payload.get('content_id') is None
