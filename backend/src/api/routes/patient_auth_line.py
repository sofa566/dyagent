from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import DialysisSession, FollowUpCase, MonitoringRecord, RenalPatient, User
from src.services.patient_portal_auth_service import (
    PATIENT_PORTAL_LINK_TTL_SECONDS_DEFAULT,
    patient_portal_auth_service,
)
from src.services.renal_monitoring_service import renal_monitoring_service

router = APIRouter()


def _require_renal_manage_permission(*, current_user: User, db: Session) -> None:
    can_manage = bool(check_permission(current_user, 'update_agent', db=db) or check_permission(current_user, 'read_logs', db=db))
    if not can_manage:
        raise forbidden_error('無權限管理腎友入口連結')


@router.post('/patient-auth/line/link/request')
async def request_patient_magic_link(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_manage_permission(current_user=current_user, db=db)
    return patient_portal_auth_service.request_magic_link(
        db=db,
        patient_code=str((payload or {}).get('patient_id') or '').strip(),
        line_user_id=str((payload or {}).get('line_user_id') or '').strip(),
        ttl_seconds=int((payload or {}).get('ttl_seconds') or PATIENT_PORTAL_LINK_TTL_SECONDS_DEFAULT),
        reason=str((payload or {}).get('reason') or '').strip(),
        created_by_user_id=str(current_user.id),
    )


@router.post('/patient-auth/line/login')
async def login_patient_magic_link(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    return patient_portal_auth_service.login_with_magic_token(db=db, token=str((payload or {}).get('token') or '').strip())


@router.post('/patient-auth/line/link/resend')
async def resend_patient_magic_link(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_manage_permission(current_user=current_user, db=db)
    return patient_portal_auth_service.resend_magic_link(
        db=db,
        patient_code=str((payload or {}).get('patient_id') or '').strip(),
        reason=str((payload or {}).get('reason') or '').strip() or 'manual_resend',
        created_by_user_id=str(current_user.id),
    )


@router.post('/patient-auth/line/link/revoke')
async def revoke_patient_magic_link(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_manage_permission(current_user=current_user, db=db)
    return patient_portal_auth_service.revoke_link(db=db, link_id=str((payload or {}).get('link_id') or '').strip())


def _require_patient_session(*, x_patient_session: str) -> dict:
    token = str(x_patient_session or '').strip()
    if not token:
        raise forbidden_error('缺少病患工作台 session token')
    return patient_portal_auth_service.decode_patient_session_token(token=token)


@router.get('/patient-portal/me/summary')
async def get_patient_portal_summary(
    x_patient_session: str = Header(default=''),
    db: Session = Depends(get_db),
):
    # 目的：回傳病患工作台首屏需要的病患主檔與最近量測摘要。
    # 為什麼：頁面重整後若只有 session token，仍需可直接恢復顯示名稱與關鍵指標。
    payload = _require_patient_session(x_patient_session=x_patient_session)
    patient_code = str(payload.get('patient_id') or '').strip()
    patient = db.query(RenalPatient).filter(RenalPatient.patient_code == patient_code, RenalPatient.enabled == True).first()  # noqa: E712
    if patient is None:
        raise forbidden_error('病患 session 對應主檔不存在或已停用')

    latest_record = (
        db.query(MonitoringRecord)
        .filter(MonitoringRecord.patient_id == patient.id)
        .order_by(MonitoringRecord.recorded_at.desc())
        .first()
    )

    return {
        'ok': True,
        'patient': {
            'id': str(patient.patient_code),
            'display_name': str(patient.display_name or patient.patient_code),
            'primary_nurse_id': str(patient.primary_nurse_id or ''),
            'diagnosis': str(patient.diagnosis or ''),
            'dry_weight_kg': float(patient.dry_weight_kg) if patient.dry_weight_kg is not None else None,
            'line_user_id': str(payload.get('line_user_id') or ''),
        },
        'latest_monitoring': {
            'recorded_at': latest_record.recorded_at.isoformat() if latest_record and latest_record.recorded_at else None,
            'weight_kg': (latest_record.measurements or {}).get('weight_kg') if latest_record else None,
            'bp': _format_bp((latest_record.measurements or {}) if latest_record else {}),
            'follow_up_required': bool(getattr(latest_record, 'follow_up_required', False)),
        }
        if latest_record is not None
        else None,
    }


@router.get('/patient-portal/me/monitoring-records')
async def list_patient_portal_monitoring_records(
    days: int = 7,
    x_patient_session: str = Header(default=''),
    db: Session = Depends(get_db),
):
    payload = _require_patient_session(x_patient_session=x_patient_session)
    patient_id = str(payload.get('patient_id') or '').strip()
    return renal_monitoring_service.list_monitoring_records(db=db, patient_code=patient_id, days=days)


@router.get('/patient-portal/me/dashboard')
async def get_patient_portal_dashboard(
    days: int = 7,
    x_patient_session: str = Header(default=''),
    db: Session = Depends(get_db),
):
    # 目的：提供病患單頁工作台一次取得所需資料。
    # 為什麼：單頁儀表板若拆成多次請求，容易產生資料時間落差與首屏延遲。
    payload = _require_patient_session(x_patient_session=x_patient_session)
    patient_code = str(payload.get('patient_id') or '').strip()
    patient = db.query(RenalPatient).filter(RenalPatient.patient_code == patient_code, RenalPatient.enabled == True).first()  # noqa: E712
    if patient is None:
        raise forbidden_error('病患 session 對應主檔不存在或已停用')

    safe_days = max(1, min(int(days or 7), 30))
    records_payload = renal_monitoring_service.list_monitoring_records(db=db, patient_code=patient_code, days=safe_days)
    records = records_payload.get('records') if isinstance(records_payload, dict) else []
    open_follow_up_rows = (
        db.query(FollowUpCase)
        .filter(FollowUpCase.patient_id == patient.id, FollowUpCase.status != 'CLOSED')
        .order_by(FollowUpCase.updated_at.desc())
        .all()
    )
    follow_up_count = (
        db.query(FollowUpCase)
        .filter(FollowUpCase.patient_id == patient.id, FollowUpCase.status != 'CLOSED')
        .count()
    )
    latest_session = (
        db.query(DialysisSession)
        .filter(DialysisSession.patient_id == patient.id)
        .order_by(DialysisSession.dialysis_date.desc(), DialysisSession.updated_at.desc())
        .first()
    )

    return {
        'ok': True,
        'patient': {
            'id': str(patient.patient_code),
            'display_name': str(patient.display_name or patient.patient_code),
            'tel_no': str(patient.tel_no or ''),
            'id_card': str(patient.id_card or ''),
            'med_history': str(patient.med_history or ''),
            'line_user_id': str(payload.get('line_user_id') or ''),
            'diagnosis': str(patient.diagnosis or ''),
            'dry_weight_kg': float(patient.dry_weight_kg) if patient.dry_weight_kg is not None else None,
        },
        'metrics': {
            'monitoring_record_count': len(records if isinstance(records, list) else []),
            'open_follow_up_count': int(follow_up_count),
            'latest_bp': (records[0] if isinstance(records, list) and records else {}).get('bp') if isinstance(records, list) else '',
            'latest_weight_kg': (records[0] if isinstance(records, list) and records else {}).get('weight_kg') if isinstance(records, list) else None,
            'latest_pulse': _extract_latest_pulse(db=db, patient_id=str(patient.id)),
        },
        'latest_session': {
            'dialysis_date': latest_session.dialysis_date.isoformat() if latest_session and latest_session.dialysis_date else None,
            'shift': str(getattr(latest_session, 'shift', '') or ''),
            'bed_no': str(getattr(latest_session, 'bed_no', '') or ''),
            'machine_no': str(getattr(latest_session, 'machine_no', '') or ''),
            'status': str(getattr(latest_session, 'completion_status', '') or ''),
            'pre_weight_kg': float(latest_session.pre_weight_kg) if latest_session and latest_session.pre_weight_kg is not None else None,
            'post_weight_kg': float(latest_session.post_weight_kg) if latest_session and latest_session.post_weight_kg is not None else None,
            'target_uf_l': float(latest_session.target_uf_l) if latest_session and latest_session.target_uf_l is not None else None,
            'actual_uf_l': float(latest_session.actual_uf_l) if latest_session and latest_session.actual_uf_l is not None else None,
        }
        if latest_session is not None
        else None,
        'records': records if isinstance(records, list) else [],
        'open_follow_ups': [
            {
                'id': str(row.id),
                'source_type': str(row.source_type or ''),
                'severity': str(row.severity or ''),
                'status': str(row.status or ''),
                'title': str(row.title or ''),
                'details': dict(row.details or {}),
                'updated_at': row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in open_follow_up_rows
        ],
        'today_tasks': _build_today_tasks_payload(latest_session=latest_session, open_follow_up_count=int(follow_up_count)),
        'education_recommendations': _build_education_recommendations(patient=patient, open_follow_up_rows=open_follow_up_rows),
    }


def _format_bp(measurements: dict) -> str:
    systolic = measurements.get('systolic') if isinstance(measurements, dict) else None
    diastolic = measurements.get('diastolic') if isinstance(measurements, dict) else None
    if systolic is None or diastolic is None:
        return ''
    return f'{systolic}/{diastolic}'


def _extract_latest_pulse(*, db: Session, patient_id: str) -> int | None:
    latest_record = (
        db.query(MonitoringRecord)
        .filter(MonitoringRecord.patient_id == patient_id)
        .order_by(MonitoringRecord.recorded_at.desc())
        .first()
    )
    pulse_value = (latest_record.measurements or {}).get('pulse') if latest_record is not None else None
    try:
        return int(pulse_value) if pulse_value is not None else None
    except Exception:
        return None


def _build_today_tasks_payload(*, latest_session: DialysisSession | None, open_follow_up_count: int) -> list[dict]:
    # 目的：輸出病患頁「今天要完成」卡片所需任務清單。
    # 為什麼：讓前端免寫硬編碼任務，直接由後端狀態驅動完成度。
    has_session = latest_session is not None
    finished_status = {'COMPLETED', 'ENDED_EARLY'}
    current_status = str(getattr(latest_session, 'completion_status', '') or '') if has_session else ''
    morning_done = bool(has_session and current_status in {'PRE_CHECK_CONFIRMED', 'IN_PROGRESS'} | finished_status)
    evening_done = bool(has_session and current_status in finished_status)
    follow_up_text = '已有追蹤事件，請持續回覆主觀狀況。' if open_follow_up_count > 0 else '如有不適請立即透過 LINE 回報。'
    return [
        {'task_id': 'morning_checkin', 'title': '早晨健康填報', 'status': 'done' if morning_done else 'pending', 'hint': '06:00 提醒'},
        {'task_id': 'evening_checkin', 'title': '晚間健康填報', 'status': 'done' if evening_done else 'pending', 'hint': '21:00 LINE 提醒'},
        {'task_id': 'followup_notice', 'title': '症狀即時回報', 'status': 'done' if open_follow_up_count == 0 else 'pending', 'hint': follow_up_text},
    ]


def _build_education_recommendations(*, patient: RenalPatient, open_follow_up_rows: list[FollowUpCase]) -> list[dict]:
    # 目的：依病患屬性與追蹤狀態提供衛教推薦資料。
    # 為什麼：病患頁需可直接渲染推薦卡，不應把規則硬寫在前端。
    recommendations = [
        {
            'education_id': 'ED-FALL-01',
            'title': '洗後頭暈與預防跌倒',
            'duration': '3:20',
            'reason': '若有頭暈或站立不穩，建議優先觀看。',
        },
        {
            'education_id': 'ED-FLUID-02',
            'title': '看懂透析間體重與飲水管理',
            'duration': '4:10',
            'reason': '協助理解體重變化與飲水控制。',
        },
    ]
    if bool(getattr(patient, 'is_diabetic', False)):
        recommendations.append(
            {
                'education_id': 'ED-FOOT-03',
                'title': '糖尿病足部每日檢查',
                'duration': '5:05',
                'reason': '糖尿病病患建議每日觀察足部與傷口變化。',
            }
        )
    if any(str(getattr(row, 'severity', '') or '') == 'critical' for row in open_follow_up_rows):
        recommendations.insert(
            0,
            {
                'education_id': 'ED-ALERT-99',
                'title': '出現危險徵象時如何立即求助',
                'duration': '2:40',
                'reason': '目前有高優先追蹤案件，建議先看緊急應對內容。',
            },
        )
    return recommendations
