from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import (
    DialysisEvent,
    DialysisSession,
    FollowUpCase,
    MonitoringRecord,
    RenalPatient,
    User,
)
from src.services.renal_demo_data_service import renal_demo_data_service

router = APIRouter()


def _has_any_permission(*, current_user: User, db: Session, permission_keys: list[str]) -> bool:
    return any(check_permission(current_user, permission_key, db=db) for permission_key in permission_keys)


def _require_renal_nurse_permission(*, current_user: User, db: Session, permission_keys: list[str]) -> None:
    can_access = _has_any_permission(current_user=current_user, db=db, permission_keys=permission_keys)
    if not can_access:
        raise forbidden_error('無權限檢視腎友照護儀表板')


def _require_renal_manage_permission(*, current_user: User, db: Session) -> None:
    can_manage = _has_any_permission(
        current_user=current_user,
        db=db,
        permission_keys=['renal.create', 'read_logs', 'update_agent'],
    )
    if not can_manage:
        raise forbidden_error('無權限建立腎友展示資料')


@router.post('/dashboard/renal/demo/bootstrap')
async def bootstrap_renal_demo_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：建立腎友照護 POC 所需展示資料。
    # 為什麼：前端驗收與流程演示需穩定樣本，避免每次手動填報。
    _require_renal_manage_permission(current_user=current_user, db=db)
    return renal_demo_data_service.bootstrap_demo_data(db=db)


@router.get('/dashboard/renal/summary')
async def get_renal_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(
        current_user=current_user,
        db=db,
        permission_keys=['nursing.overview', 'read_logs', 'update_agent'],
    )
    total_patients = db.query(RenalPatient).filter(RenalPatient.enabled == True).count()  # noqa: E712
    open_follow_ups = db.query(FollowUpCase).filter(FollowUpCase.status != 'CLOSED').count()
    completed_sessions = db.query(DialysisSession).filter(DialysisSession.completion_status == 'COMPLETED').count()
    return {
        'ok': True,
        'summary': {
            'total_patients': int(total_patients),
            'need_follow_up': int(open_follow_ups),
            'completed_sessions': int(completed_sessions),
        },
    }


@router.get('/dashboard/renal/patients')
async def get_renal_patients(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：提供護理師工作台四人總覽所需資料。
    # 為什麼：前端需要單次查詢得到病患卡片與最新監測/療程摘要。
    _require_renal_nurse_permission(
        current_user=current_user,
        db=db,
        permission_keys=['nursing.overview', 'read_logs', 'update_agent'],
    )
    patient_rows = db.query(RenalPatient).filter(RenalPatient.enabled == True).order_by(RenalPatient.patient_code.asc()).all()  # noqa: E712
    output = []
    for patient in patient_rows:
        latest_monitoring = (
            db.query(MonitoringRecord)
            .filter(MonitoringRecord.patient_id == patient.id)
            .order_by(MonitoringRecord.recorded_at.desc())
            .first()
        )
        latest_session = (
            db.query(DialysisSession)
            .filter(DialysisSession.patient_id == patient.id)
            .order_by(DialysisSession.dialysis_date.desc(), DialysisSession.updated_at.desc())
            .first()
        )
        latest_event = _find_latest_session_event(db=db, session=latest_session)
        open_follow_up_rows = (
            db.query(FollowUpCase)
            .filter(FollowUpCase.patient_id == patient.id, FollowUpCase.status != 'CLOSED')
            .order_by(FollowUpCase.updated_at.desc())
            .all()
        )
        status_payload = _build_patient_status_payload(
            latest_session=latest_session,
            latest_monitoring=latest_monitoring,
            latest_event=latest_event,
            open_follow_up_rows=open_follow_up_rows,
        )
        tags = _build_patient_tags(
            patient=patient,
            latest_monitoring=latest_monitoring,
            latest_session=latest_session,
            open_follow_up_rows=open_follow_up_rows,
        )

        output.append(
            {
                'patient_id': str(patient.patient_code),
                'name': str(patient.display_name or ''),
                'tel_no': str(patient.tel_no or ''),
                'id_card': str(patient.id_card or ''),
                'med_history': str(patient.med_history or ''),
                'age': str(patient.age or ''),
                'diagnosis': str(patient.diagnosis or ''),
                'primary_nurse_id': str(patient.primary_nurse_id or ''),
                'is_diabetic': bool(patient.is_diabetic),
                'dry_weight_kg': float(patient.dry_weight_kg) if patient.dry_weight_kg is not None else None,
                'line_user_bound': bool(str(patient.line_user_id or '').strip()),
                'status': status_payload,
                'open_follow_up_count': len(open_follow_up_rows),
                'tags': tags,
                'latest_monitoring': {
                    'recorded_at': latest_monitoring.recorded_at.isoformat() if latest_monitoring and latest_monitoring.recorded_at else None,
                    'bp': _format_bp((latest_monitoring.measurements if latest_monitoring else {}) or {}),
                    'weight_kg': (latest_monitoring.measurements if latest_monitoring else {}).get('weight_kg'),
                    'pulse': (latest_monitoring.measurements if latest_monitoring else {}).get('pulse'),
                    'systolic': (latest_monitoring.measurements if latest_monitoring else {}).get('systolic'),
                    'diastolic': (latest_monitoring.measurements if latest_monitoring else {}).get('diastolic'),
                    'symptoms': (latest_monitoring.symptoms if latest_monitoring else {}) or {},
                    'follow_up_required': bool(getattr(latest_monitoring, 'follow_up_required', False)),
                }
                if latest_monitoring is not None
                else None,
                'latest_dialysis': {
                    'session_id': str(getattr(latest_session, 'id', '') or ''),
                    'dialysis_date': latest_session.dialysis_date.isoformat() if latest_session and latest_session.dialysis_date else None,
                    'shift': str(getattr(latest_session, 'shift', '') or ''),
                    'bed_no': str(getattr(latest_session, 'bed_no', '') or ''),
                    'machine_no': str(getattr(latest_session, 'machine_no', '') or ''),
                    'paired': bool(str(getattr(latest_session, 'bed_no', '') or '').strip() and str(getattr(latest_session, 'machine_no', '') or '').strip()),
                    'status': str(getattr(latest_session, 'completion_status', '') or ''),
                    'pre_weight_kg': float(latest_session.pre_weight_kg) if latest_session and latest_session.pre_weight_kg is not None else None,
                    'post_weight_kg': float(latest_session.post_weight_kg) if latest_session and latest_session.post_weight_kg is not None else None,
                    'target_uf_l': float(latest_session.target_uf_l) if latest_session and latest_session.target_uf_l is not None else None,
                    'actual_uf_l': float(latest_session.actual_uf_l) if latest_session and latest_session.actual_uf_l is not None else None,
                    'note': str(getattr(latest_session, 'note', '') or ''),
                    'machine_time': latest_session.updated_at.strftime('%H:%M:%S') if latest_session and latest_session.updated_at else None,
                    'progress_percent': _build_session_progress_percent(latest_session=latest_session),
                }
                if latest_session is not None
                else None,
                'latest_event': {
                    'event_type': str(getattr(latest_event, 'event_type', '') or ''),
                    'event_at': latest_event.event_at.isoformat() if latest_event and latest_event.event_at else None,
                    'action': str(getattr(latest_event, 'action', '') or ''),
                    'payload': dict(getattr(latest_event, 'payload', {}) or {}),
                }
                if latest_event is not None
                else None,
            }
        )
    return {'ok': True, 'patients': output}


@router.get('/dashboard/renal/follow-ups')
async def get_renal_follow_ups(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_nurse_permission(
        current_user=current_user,
        db=db,
        permission_keys=['nursing.tracing', 'read_logs', 'update_agent'],
    )
    safe_limit = max(1, min(int(limit or 50), 200))
    rows = (
        db.query(FollowUpCase, RenalPatient)
        .join(RenalPatient, FollowUpCase.patient_id == RenalPatient.id)
        .order_by(FollowUpCase.updated_at.desc())
        .limit(safe_limit)
        .all()
    )
    return {
        'ok': True,
        'follow_ups': [
            {
                'id': str(case.id),
                'patient_id': str(patient.patient_code),
                'patient_name': str(patient.display_name or ''),
                'source_type': str(case.source_type or ''),
                'severity': str(case.severity or ''),
                'status': str(case.status or ''),
                'title': str(case.title or ''),
                'assigned_nurse_id': str(case.assigned_nurse_id or ''),
                'details': dict(case.details or {}),
                'source_snapshot': _build_follow_up_source_snapshot(db=db, case=case),
                'updated_at': case.updated_at.isoformat() if case.updated_at else None,
            }
            for case, patient in rows
        ],
    }


def _find_latest_session_event(*, db: Session, session: DialysisSession | None) -> DialysisEvent | None:
    if session is None:
        return None
    return db.query(DialysisEvent).filter(DialysisEvent.session_id == session.id).order_by(DialysisEvent.event_at.desc()).first()


def _build_session_progress_percent(*, latest_session: DialysisSession | None) -> int:
    # 目的：計算療程進度百分比供卡片顯示。
    # 為什麼：前端需統一來源計算進度，避免不同頁面算出不同數值。
    if latest_session is None:
        return 0
    target_uf = float(latest_session.target_uf_l) if latest_session.target_uf_l is not None else 0.0
    actual_uf = float(latest_session.actual_uf_l) if latest_session.actual_uf_l is not None else 0.0
    if target_uf > 0 and actual_uf >= 0:
        return max(0, min(int(round((actual_uf / target_uf) * 100)), 100))
    status = str(latest_session.completion_status or '')
    if status in {'COMPLETED', 'ENDED_EARLY'}:
        return 100
    if status in {'PRE_CHECK_CONFIRMED', 'IN_PROGRESS'}:
        return 70
    return 0


def _build_patient_status_payload(
    *,
    latest_session: DialysisSession | None,
    latest_monitoring: MonitoringRecord | None,
    latest_event: DialysisEvent | None,
    open_follow_up_rows: list[FollowUpCase],
) -> dict:
    # 目的：統一輸出病患卡片的狀態文字與色階。
    # 為什麼：前端需用同一欄位驅動 badge/tone，避免分散在多個判斷規則。
    if any(str(getattr(row, 'severity', '') or '') == 'critical' for row in open_follow_up_rows):
        return {'tone': 'red', 'label': '緊急追蹤', 'reason': '存在 critical 追蹤案件'}

    if open_follow_up_rows:
        return {'tone': 'amber', 'label': '事件追蹤中', 'reason': '存在未結案追蹤案件'}

    session_status = str(getattr(latest_session, 'completion_status', '') or '')
    if session_status == 'ENDED_EARLY':
        return {'tone': 'red', 'label': '提前結束', 'reason': '透析完成狀態為 ENDED_EARLY'}
    if session_status == 'COMPLETED':
        return {'tone': 'green', 'label': '正常完成', 'reason': '透析完成狀態為 COMPLETED'}

    if latest_event is not None:
        return {'tone': 'amber', 'label': '療程觀察中', 'reason': f'最新事件 {latest_event.event_type}'}
    if latest_monitoring is not None and bool(latest_monitoring.follow_up_required):
        return {'tone': 'amber', 'label': '監測需追蹤', 'reason': '最近監測命中追蹤規則'}
    return {'tone': 'blue', 'label': '待觀察', 'reason': '目前未有完成療程或異常事件'}


def _build_patient_tags(
    *,
    patient: RenalPatient,
    latest_monitoring: MonitoringRecord | None,
    latest_session: DialysisSession | None,
    open_follow_up_rows: list[FollowUpCase],
) -> list[str]:
    # 目的：彙整病患卡片標籤（共病、症狀、療程與追蹤）。
    # 為什麼：標籤能快速提示護理師注意重點，減少逐欄閱讀成本。
    tags: list[str] = []
    if bool(getattr(patient, 'is_diabetic', False)):
        tags.append('糖尿病')

    symptoms = (latest_monitoring.symptoms if latest_monitoring is not None else {}) or {}
    if bool(symptoms.get('fall')):
        tags.append('摔倒追蹤')
    if bool(symptoms.get('wound_changed')):
        tags.append('傷口追蹤')
    if bool(symptoms.get('breathing_discomfort')):
        tags.append('呼吸不適')

    status_value = str(getattr(latest_session, 'completion_status', '') or '')
    if status_value == 'ENDED_EARLY':
        tags.append('提前結束')
    if status_value == 'COMPLETED':
        tags.append('已完成')
    if open_follow_up_rows:
        tags.append('需主護追蹤')
    return tags


def _build_follow_up_source_snapshot(*, db: Session, case: FollowUpCase) -> dict:
    # 目的：根據追蹤來源補齊可讀證據摘要。
    # 為什麼：異常追蹤頁需要同時看到案件與原始依據，才能快速處置。
    source_type = str(case.source_type or '')
    if source_type == 'dialysis':
        event = db.query(DialysisEvent).filter(DialysisEvent.id == case.source_record_id).first()
        if event is None:
            return {'type': 'dialysis', 'missing': True}
        session = db.query(DialysisSession).filter(DialysisSession.id == event.session_id).first()
        return {
            'type': 'dialysis',
            'event_type': str(event.event_type or ''),
            'event_at': event.event_at.isoformat() if event.event_at else None,
            'action': str(event.action or ''),
            'payload': dict(event.payload or {}),
            'bed_no': str(getattr(session, 'bed_no', '') or ''),
            'machine_no': str(getattr(session, 'machine_no', '') or ''),
        }

    if source_type == 'monitoring':
        record = db.query(MonitoringRecord).filter(MonitoringRecord.id == case.source_record_id).first()
        if record is None:
            return {'type': 'monitoring', 'missing': True}
        return {
            'type': 'monitoring',
            'recorded_at': record.recorded_at.isoformat() if record.recorded_at else None,
            'record_type': str(record.record_type or ''),
            'bp': _format_bp(record.measurements or {}),
            'weight_kg': (record.measurements or {}).get('weight_kg'),
            'symptoms': dict(record.symptoms or {}),
        }

    return {'type': source_type}


def _format_bp(measurements: dict) -> str:
    systolic = measurements.get('systolic') if isinstance(measurements, dict) else None
    diastolic = measurements.get('diastolic') if isinstance(measurements, dict) else None
    if systolic is None or diastolic is None:
        return ''
    return f'{systolic}/{diastolic}'
