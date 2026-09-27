from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

from src.models import Conversation, LineChannelSession, LineMessage, MonitoringRecord, RenalPatient
from src.services.monitoring_reminder_service import monitoring_reminder_service


def _create_demo_patient(
    db,
    *,
    patient_code: str = 'P001',
    line_user_id: str | None = 'U_RENAL_PATIENT_001',
    tel_no: str | None = '0910000000',
):
    # 目的：建立測試用腎友主檔，並允許覆寫 LINE 與手機欄位。
    # 為什麼：不同情境需驗證綁定判定邏輯，需可精準控制 line_user_id 與 tel_no 組合。
    row = RenalPatient(
        patient_code=patient_code,
        display_name='王美華',
        tel_no=tel_no,
        diagnosis='糖尿病腎病',
        primary_nurse_id='N001',
        is_diabetic=True,
        dry_weight_kg=63.0,
        line_user_id=line_user_id,
        enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_create_monitoring_record_and_follow_up(client, db, admin_token):
    # 目的：驗證 S01 可寫入監測並於異常時建立追蹤案件。
    # 為什麼：日常監測到追蹤建立是腎友照護 POC 最小閉環核心。
    _create_demo_patient(db)

    response = client.post(
        '/api/monitoring-records',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'patient_id': 'P001',
            'record_type': 'MORNING',
            'recorded_at': datetime.now().isoformat(),
            'submitted_by_role': 'PATIENT',
            'measurements': {'weight_kg': 66.4, 'systolic': 176, 'diastolic': 96, 'pulse': 88, 'blood_glucose_mg_dl': 256},
            'symptoms': {'wound_changed': True},
            'confirmed': True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload.get('ok') is True
    assert payload.get('record_status') == 'SAVED'
    assert payload.get('follow_up', {}).get('required') is True


def test_diabetic_monitoring_requires_glucose(client, db, admin_token):
    # 目的：驗證糖尿病腎友早晚回報缺血糖時會被拒絕。
    # 為什麼：糖友監測規則需在後端強制，避免前端漏填造成資料風險。
    _create_demo_patient(db, patient_code='P021', line_user_id='U_RENAL_PATIENT_021')

    response = client.post(
        '/api/monitoring-records',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'patient_id': 'P021',
            'record_type': 'EVENING',
            'recorded_at': datetime.now().isoformat(),
            'submitted_by_role': 'PATIENT',
            'measurements': {'weight_kg': 64.4, 'systolic': 132, 'diastolic': 78, 'pulse': 72},
            'symptoms': {},
            'confirmed': True,
        },
    )

    assert response.status_code == 400
    assert 'blood_glucose_mg_dl' in str(response.json().get('detail') or '')


def test_non_diabetic_monitoring_allows_missing_glucose(client, db, admin_token):
    # 目的：驗證非糖尿病腎友可不填血糖。
    # 為什麼：血糖必填只適用糖友，避免對非糖友造成不必要阻擋。
    patient = _create_demo_patient(db, patient_code='P022', line_user_id='U_RENAL_PATIENT_022')
    patient.is_diabetic = False
    db.add(patient)
    db.commit()

    response = client.post(
        '/api/monitoring-records',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'patient_id': 'P022',
            'record_type': 'EVENING',
            'recorded_at': datetime.now().isoformat(),
            'submitted_by_role': 'PATIENT',
            'measurements': {'weight_kg': 64.0, 'systolic': 128, 'diastolic': 74, 'pulse': 70},
            'symptoms': {},
            'confirmed': True,
        },
    )

    assert response.status_code == 200
    assert response.json().get('record_status') == 'SAVED'


def test_patient_magic_link_login_flow(client, db, admin_token):
    # 目的：驗證腎友可透過 LINE magic link 免註冊登入病患工作台。
    # 為什麼：腎友入口不依賴平台帳密是本次 POC 的重要需求。
    _create_demo_patient(db, patient_code='P009', line_user_id='U_RENAL_PATIENT_009')

    create_response = client.post(
        '/api/patient-auth/line/link/request',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'patient_id': 'P009',
            'line_user_id': 'U_RENAL_PATIENT_009',
            'ttl_seconds': 600,
            'reason': 'manual_support',
        },
    )
    assert create_response.status_code == 200
    magic_link = str(create_response.json().get('magic_link') or '')
    assert '?token=' in magic_link
    token = magic_link.split('?token=', 1)[1]

    login_response = client.post('/api/patient-auth/line/login', json={'token': token})
    assert login_response.status_code == 200
    session_token = str(login_response.json().get('session_token') or '')
    assert session_token

    me_response = client.get('/api/patient-portal/me/summary', headers={'X-Patient-Session': session_token})
    assert me_response.status_code == 200
    assert me_response.json().get('patient', {}).get('id') == 'P009'


def test_bootstrap_demo_data_api_creates_dashboard_data(client, admin_token):
    # 目的：驗證展示資料 bootstrap API 可建立護理師與病患頁所需樣本。
    # 為什麼：開發階段沒有即時來源時，需有可重覆產生的測試資料流程。
    bootstrap_response = client.post(
        '/api/dashboard/renal/demo/bootstrap',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert bootstrap_response.status_code == 200
    payload = bootstrap_response.json()
    assert payload.get('ok') is True
    assert int(payload.get('patient_count') or 0) == 4
    assert int(payload.get('created_monitoring_records') or 0) > 0

    dashboard_response = client.get('/api/dashboard/renal/patients', headers={'Authorization': f'Bearer {admin_token}'})
    assert dashboard_response.status_code == 200
    patients = dashboard_response.json().get('patients') or []
    assert len(patients) == 4
    first_patient = patients[0]
    assert first_patient.get('latest_monitoring') is not None
    assert first_patient.get('status') is not None
    assert isinstance(first_patient.get('tags'), list)
    assert first_patient.get('latest_dialysis', {}).get('machine_no')
    assert 'paired' in (first_patient.get('latest_dialysis') or {})
    assert 'pre_weight_kg' in (first_patient.get('latest_dialysis') or {})
    assert 'target_uf_l' in (first_patient.get('latest_dialysis') or {})


def test_patient_dashboard_api_returns_enriched_payload(client, db, admin_token):
    # 目的：驗證病患單頁儀表板 API 可一次回傳病患資料、摘要指標與近七日記錄。
    # 為什麼：前端單頁需單一請求就完成首屏渲染，避免多次請求造成不同步。
    _create_demo_patient(db, patient_code='P020', line_user_id='U_RENAL_PATIENT_020')
    monitoring_response = client.post(
        '/api/monitoring-records',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'patient_id': 'P020',
            'record_type': 'MORNING',
            'recorded_at': datetime.now().isoformat(),
            'submitted_by_role': 'PATIENT',
            'measurements': {'weight_kg': 66.1, 'systolic': 174, 'diastolic': 90, 'pulse': 80, 'blood_glucose_mg_dl': 198},
            'symptoms': {},
            'confirmed': True,
        },
    )
    assert monitoring_response.status_code == 200

    create_response = client.post(
        '/api/patient-auth/line/link/request',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'patient_id': 'P020',
            'line_user_id': 'U_RENAL_PATIENT_020',
            'ttl_seconds': 600,
            'reason': 'dashboard_test',
        },
    )
    token = str(create_response.json().get('magic_link') or '').split('?token=', 1)[1]
    login_response = client.post('/api/patient-auth/line/login', json={'token': token})
    session_token = str(login_response.json().get('session_token') or '')

    summary_response = client.get('/api/patient-portal/me/summary', headers={'X-Patient-Session': session_token})
    assert summary_response.status_code == 200
    assert summary_response.json().get('patient', {}).get('display_name') == '王美華'

    dashboard_response = client.get('/api/patient-portal/me/dashboard', headers={'X-Patient-Session': session_token})
    assert dashboard_response.status_code == 200
    dashboard_payload = dashboard_response.json()
    assert dashboard_payload.get('patient', {}).get('id') == 'P020'
    assert int(dashboard_payload.get('metrics', {}).get('monitoring_record_count') or 0) >= 1
    assert 'today_tasks' in dashboard_payload
    assert 'education_recommendations' in dashboard_payload
    assert isinstance(dashboard_payload.get('records'), list)


def test_renal_management_crud_and_search(client, admin_token):
    # 目的：驗證腎友管理 API 支援新增、查詢、編輯、刪除流程。
    # 為什麼：腎友管理頁需要完整 CRUD 能力，避免只能讀取展示資料。
    create_response = client.post(
        '/api/renals',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'id': 'P888',
            'name': '測試病患',
            'tel_no': '0912345678',
            'id_card': 'Z123456789',
            'med_history': '高血壓病史',
            'line_user_id': 'U_P888',
        },
    )
    assert create_response.status_code == 200

    list_response = client.get(
        '/api/renals?search_field=name&keyword=測試',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert list_response.status_code == 200
    items = list_response.json().get('items') or []
    assert any(item.get('id') == 'P888' for item in items)

    update_response = client.put(
        '/api/renals/P888',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'name': '測試病患二號',
            'tel_no': '0999888777',
            'id_card': 'Z123456789',
            'med_history': '高血壓、糖尿病',
        },
    )
    assert update_response.status_code == 200

    by_tel_response = client.get(
        '/api/renals?search_field=tel_no&keyword=0999',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert by_tel_response.status_code == 200
    by_tel_items = by_tel_response.json().get('items') or []
    assert any(item.get('name') == '測試病患二號' for item in by_tel_items)
    updated_item = next(item for item in by_tel_items if item.get('name') == '測試病患二號')
    assert str(updated_item.get('line_user_id') or '') == 'U_P888'

    delete_response = client.delete('/api/renals/P888', headers={'Authorization': f'Bearer {admin_token}'})
    assert delete_response.status_code == 200

    list_after_delete = client.get(
        '/api/renals?search_field=id_card&keyword=Z123456789',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert list_after_delete.status_code == 200
    assert (list_after_delete.json().get('items') or []) == []


def test_renal_endpoints_forbidden_for_regular_user(client, regular_user_token):
    # 目的：驗證一般使用者無法直接存取腎友管理與照護 API。
    # 為什麼：避免僅具 chat 權限的帳號讀取腎友敏感資料。
    summary_response = client.get('/api/dashboard/renal/summary', headers={'Authorization': f'Bearer {regular_user_token}'})
    assert summary_response.status_code == 403

    renals_response = client.get('/api/renals?search_field=name&keyword=', headers={'Authorization': f'Bearer {regular_user_token}'})
    assert renals_response.status_code == 403

    monitoring_response = client.get('/api/monitoring-records?patient_id=P001&days=7', headers={'Authorization': f'Bearer {regular_user_token}'})
    assert monitoring_response.status_code == 403


def test_monitoring_compliance_and_dispatch_reminder(client, db, admin_token, agent):
    # 目的：驗證每日完整性檢核與催報派送 API 可正常運作。
    # 為什麼：早晚回報規則需要可查詢與可觸發催報的最小閉環。
    _create_demo_patient(db, patient_code='P030', line_user_id='U_RENAL_PATIENT_030')

    compliance_response = client.get(
        f"/api/monitoring-compliance/daily?date={datetime.now().date().isoformat()}",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert compliance_response.status_code == 200
    items = compliance_response.json().get('patients') or []
    patient_item = next(item for item in items if item.get('patient_id') == 'P030')
    assert patient_item.get('morning_done') is False
    assert patient_item.get('evening_done') is False

    with patch.object(monitoring_reminder_service, '_push_line_message', new=AsyncMock(return_value=None)):
        dispatch_response = client.post(
            '/api/monitoring-compliance/reminders/dispatch',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'date': datetime.now().date().isoformat(), 'window': 'MORNING'},
        )

    assert dispatch_response.status_code == 200
    payload = dispatch_response.json()
    assert int(payload.get('scheduled') or 0) >= 1
    assert int(payload.get('sent') or 0) >= 1
    reminder_message = (
        db.query(LineMessage)
        .filter(LineMessage.direction == 'outbound', LineMessage.sender_type == 'system')
        .order_by(LineMessage.created_at.desc())
        .first()
    )
    assert reminder_message is not None
    assert '格式範例' in str(getattr(reminder_message, 'content', '') or '')

    with patch.object(monitoring_reminder_service, '_push_line_message', new=AsyncMock(return_value=None)):
        second_dispatch_response = client.post(
            '/api/monitoring-compliance/reminders/dispatch',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'date': datetime.now().date().isoformat(), 'window': 'MORNING'},
        )

    assert second_dispatch_response.status_code == 200
    second_payload = second_dispatch_response.json()
    assert int(second_payload.get('scheduled') or 0) == 0


def test_monitoring_compliance_uses_active_session_binding_when_patient_line_user_missing(client, db, admin_token, agent):
    # 目的：驗證病患主檔缺 line_user_id 時，仍可透過 active session 綁定判定為已綁定。
    # 為什麼：LINE 綁定流程可能先寫入 session，再回填病患主檔，提醒頁不應誤顯示未綁定。
    patient = _create_demo_patient(db, patient_code='P040', line_user_id=None)
    conversation = Conversation(agent_id=agent.id, title='line binding fallback')
    db.add(conversation)
    db.flush()
    db.add(
        LineChannelSession(
            line_user_id='U_RENAL_PATIENT_040',
            conversation_id=conversation.id,
            assigned_agent_id=agent.id,
            bound_patient_id=patient.id,
            binding_status='bound',
            mode='bot',
            status='active',
        )
    )
    db.commit()

    compliance_response = client.get(
        f"/api/monitoring-compliance/daily?date={datetime.now().date().isoformat()}",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert compliance_response.status_code == 200
    items = compliance_response.json().get('patients') or []
    patient_item = next(item for item in items if item.get('patient_id') == 'P040')
    assert patient_item.get('line_user_bound') is True

    with patch.object(monitoring_reminder_service, '_push_line_message', new=AsyncMock(return_value=None)) as push_mock:
        dispatch_response = client.post(
            '/api/monitoring-compliance/reminders/dispatch',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'date': datetime.now().date().isoformat(), 'window': 'MORNING'},
        )

    assert dispatch_response.status_code == 200
    payload = dispatch_response.json()
    assert int(payload.get('sent') or 0) == 1
    assert int(payload.get('skipped') or 0) == 0
    assert push_mock.await_count == 1
    assert push_mock.await_args.kwargs.get('to_line_user_id') == 'U_RENAL_PATIENT_040'


def test_monitoring_compliance_marks_unverified_line_user_id_as_unbound_when_phone_missing(client, db, admin_token):
    # 目的：驗證僅有 line_user_id、缺少手機且無 bound session 時，不視為已綁定。
    # 為什麼：避免歷史測試資料或未完成驗證資料誤顯示已綁定，影響護理判讀。
    patient = _create_demo_patient(db, patient_code='P041', line_user_id='U_RENAL_PATIENT_041')
    patient.tel_no = None
    db.add(patient)
    db.commit()

    compliance_response = client.get(
        f"/api/monitoring-compliance/daily?date={datetime.now().date().isoformat()}",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert compliance_response.status_code == 200
    items = compliance_response.json().get('patients') or []
    patient_item = next(item for item in items if item.get('patient_id') == 'P041')
    assert patient_item.get('line_user_bound') is False


def test_monitoring_backfill_api_creates_records_from_split_messages(client, db, admin_token, agent):
    # 目的：驗證手動補寫可從分兩則回報重建監測紀錄。
    # 為什麼：歷史資料常有解析遺漏，需靠補帳流程修復同日同時段資料。
    patient = _create_demo_patient(db, patient_code='P031', line_user_id='U_RENAL_PATIENT_031')
    patient.is_diabetic = False
    db.add(patient)
    db.commit()

    conversation = Conversation(agent_id=agent.id, title='line backfill demo')
    db.add(conversation)
    db.flush()
    session = LineChannelSession(
        line_user_id='U_RENAL_PATIENT_031',
        conversation_id=conversation.id,
        assigned_agent_id=agent.id,
        bound_patient_id=patient.id,
        binding_status='bound',
        mode='bot',
        status='active',
    )
    db.add(session)
    db.flush()

    sample_time = datetime.now().replace(hour=9, minute=20, second=0, microsecond=0)
    db.add(
        LineMessage(
            session_id=session.id,
            conversation_id=conversation.id,
            direction='inbound',
            sender_type='user',
            content='血壓 114/74',
            created_at=sample_time,
        )
    )
    db.add(
        LineMessage(
            session_id=session.id,
            conversation_id=conversation.id,
            direction='inbound',
            sender_type='user',
            content='70.9',
            created_at=sample_time.replace(minute=22),
        )
    )
    db.commit()

    backfill_response = client.post(
        '/api/monitoring-compliance/backfill',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={'start_date': sample_time.date().isoformat(), 'end_date': sample_time.date().isoformat(), 'max_scan_rows': 1000},
    )
    assert backfill_response.status_code == 200
    backfill_payload = backfill_response.json()
    assert int(backfill_payload.get('created_records') or 0) == 1

    created_record = db.query(MonitoringRecord).filter(MonitoringRecord.patient_id == patient.id).first()
    assert created_record is not None
    measurements = created_record.measurements or {}
    assert int(measurements.get('systolic') or 0) == 114
    assert int(measurements.get('diastolic') or 0) == 74
    assert float(measurements.get('weight_kg') or 0) == 70.9

    runs_response = client.get(
        '/api/monitoring-compliance/backfill/runs?limit=5',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert runs_response.status_code == 200
    runs = runs_response.json().get('runs') or []
    assert any(run.get('run_id') == backfill_payload.get('run_id') for run in runs)


def test_monitoring_reminder_policy_and_logs_api(client, db, admin_token):
    # 目的：驗證提醒管理頁需要的 policy 與派送紀錄 API 可用。
    # 為什麼：後台 UI 必須能管理模板與查看 sent/skipped/failed 統計，才能完成營運閉環。
    patient = _create_demo_patient(db, patient_code='P032', line_user_id='U_RENAL_PATIENT_032')
    patient.is_diabetic = False
    db.add(patient)
    db.commit()

    policies_response = client.get(
        '/api/monitoring-compliance/reminders/policies',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert policies_response.status_code == 200
    policies = policies_response.json().get('policies') or []
    assert len(policies) == 2
    morning_policy = next(policy for policy in policies if policy.get('window') == 'MORNING')

    update_response = client.put(
        f"/api/monitoring-compliance/reminders/policies/{morning_policy.get('id')}",
        headers={'Authorization': f'Bearer {admin_token}'},
        json={
            'enabled': True,
            'requires_glucose_for_diabetic': True,
            'message_template': '{patient_name} 請完成{window}回報。',
        },
    )
    assert update_response.status_code == 200
    assert update_response.json().get('policy', {}).get('message_template') == '{patient_name} 請完成{window}回報。'

    logs_response = client.get(
        f"/api/monitoring-compliance/reminders/logs?date={datetime.now().date().isoformat()}&window=MORNING",
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert logs_response.status_code == 200
    summary = logs_response.json().get('summary') or {}
    assert 'total_jobs' in summary
    assert 'sent' in summary
