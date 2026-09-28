from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from src.models import DialysisEvent, DialysisSession, MonitoringRecord, RenalPatient
from src.services.renal_dialysis_service import renal_dialysis_service
from src.services.renal_monitoring_service import renal_monitoring_service

DEMO_PATIENT_PROFILES = (
    {
        'patient_code': 'P001',
        'display_name': '王美華',
        'tel_no': '0911222333',
        'id_card': 'A123456789',
        'med_history': '糖尿病、右足慢性傷口',
        'age': '68',
        'diagnosis': '糖尿病腎病',
        'primary_nurse_id': 'N001',
        'is_diabetic': True,
        'dry_weight_kg': 63.0,
        'line_user_id': 'U_RENAL_PATIENT_001',
        'bed_no': 'A01',
        'machine_no': 'HDM-A01',
    },
    {
        'patient_code': 'P002',
        'display_name': '李建國',
        'tel_no': '0922333444',
        'id_card': 'B223456789',
        'med_history': '高血壓、心血管疾病',
        'age': '72',
        'diagnosis': '心血管疾病',
        'primary_nurse_id': 'N001',
        'is_diabetic': False,
        'dry_weight_kg': 57.5,
        'line_user_id': 'U_RENAL_PATIENT_002',
        'bed_no': 'A02',
        'machine_no': 'HDM-A02',
    },
    {
        'patient_code': 'P003',
        'display_name': '陳阿玉',
        'tel_no': '0933444555',
        'id_card': 'C323456789',
        'med_history': '先天性腎臟疾病、跌倒史',
        'age': '61',
        'diagnosis': '先天性腎臟疾病',
        'primary_nurse_id': 'N001',
        'is_diabetic': False,
        'dry_weight_kg': 59.8,
        'line_user_id': 'U_RENAL_PATIENT_003',
        'bed_no': 'A03',
        'machine_no': 'HDM-A03',
    },
    {
        'patient_code': 'P004',
        'display_name': '林志明',
        'tel_no': '0944555666',
        'id_card': 'D423456789',
        'med_history': '抽筋事件追蹤',
        'age': '55',
        'diagnosis': '其他後天因素',
        'primary_nurse_id': 'N001',
        'is_diabetic': False,
        'dry_weight_kg': 68.0,
        'line_user_id': 'U_RENAL_PATIENT_004',
        'bed_no': 'A04',
        'machine_no': 'HDM-A04',
    },
)


class RenalDemoDataService:
    # 目的：建立可重覆執行的腎友照護 POC 展示資料。
    # 為什麼：前端開發與流程演示需要穩定資料，避免每次手動輸入。

    # 目的：建立/補齊四位病患、近七日監測與今日透析資料。
    # 為什麼：讓護理師與病患頁在無真實資料時仍能完整展示流程。
    def bootstrap_demo_data(self, *, db: Session) -> dict:
        created_patients = 0
        updated_patients = 0
        created_monitoring_records = 0
        created_sessions = 0
        created_events = 0
        post_checked_sessions = 0
        today = date.today()

        for profile in DEMO_PATIENT_PROFILES:
            patient_row, was_created = self._upsert_patient(db=db, profile=profile)
            if was_created:
                created_patients += 1
            else:
                updated_patients += 1

            created_monitoring_records += self._seed_monitoring_records(
                db=db,
                patient_code=str(patient_row.patient_code),
                dry_weight_kg=float(patient_row.dry_weight_kg or 0),
                is_primary_patient=(str(patient_row.patient_code) == 'P001'),
                is_diabetic=bool(getattr(patient_row, 'is_diabetic', False)),
            )
            session_metrics = self._seed_today_dialysis_session(
                db=db,
                patient_code=str(patient_row.patient_code),
                dialysis_date=today,
                bed_no=str(profile['bed_no']),
                machine_no=str(profile['machine_no']),
                dry_weight_kg=float(patient_row.dry_weight_kg or 0),
                is_primary_patient=(str(patient_row.patient_code) == 'P001'),
            )
            created_sessions += int(session_metrics['created_sessions'])
            created_events += int(session_metrics['created_events'])
            post_checked_sessions += int(session_metrics['post_checked_sessions'])

        return {
            'ok': True,
            'created_patients': created_patients,
            'updated_patients': updated_patients,
            'created_monitoring_records': created_monitoring_records,
            'created_dialysis_sessions': created_sessions,
            'created_dialysis_events': created_events,
            'post_checked_sessions': post_checked_sessions,
            'patient_count': len(DEMO_PATIENT_PROFILES),
        }

    def _upsert_patient(self, *, db: Session, profile: dict) -> tuple[RenalPatient, bool]:
        row = db.query(RenalPatient).filter(RenalPatient.patient_code == str(profile['patient_code'])).first()
        if row is None:
            created = RenalPatient(
                patient_code=str(profile['patient_code']),
                display_name=str(profile['display_name']),
                tel_no=str(profile['tel_no']),
                id_card=str(profile['id_card']),
                med_history=str(profile['med_history']),
                age=str(profile['age']),
                diagnosis=str(profile['diagnosis']),
                primary_nurse_id=str(profile['primary_nurse_id']),
                is_diabetic=bool(profile['is_diabetic']),
                dry_weight_kg=float(profile['dry_weight_kg']),
                line_user_id=str(profile['line_user_id']),
                enabled=True,
            )
            db.add(created)
            db.commit()
            db.refresh(created)
            return created, True

        row.display_name = str(profile['display_name'])
        row.tel_no = str(profile['tel_no'])
        row.id_card = str(profile['id_card'])
        row.med_history = str(profile['med_history'])
        row.age = str(profile['age'])
        row.diagnosis = str(profile['diagnosis'])
        row.primary_nurse_id = str(profile['primary_nurse_id'])
        row.is_diabetic = bool(profile['is_diabetic'])
        row.dry_weight_kg = float(profile['dry_weight_kg'])
        row.line_user_id = str(profile['line_user_id'])
        row.enabled = True
        db.add(row)
        db.commit()
        db.refresh(row)
        return row, False

    # 目的：補齊近七日監測記錄（同時間戳避免重覆建立）。
    # 為什麼：趨勢圖與病患摘要都依賴連續天資料。
    def _seed_monitoring_records(
        self,
        *,
        db: Session,
        patient_code: str,
        dry_weight_kg: float,
        is_primary_patient: bool,
        is_diabetic: bool,
    ) -> int:
        created_count = 0
        for day_offset in range(6, -1, -1):
            record_day = date.today() - timedelta(days=day_offset)
            recorded_at = datetime.combine(record_day, time(hour=6, minute=12)).replace(microsecond=0)
            exists = (
                db.query(MonitoringRecord)
                .join(RenalPatient, MonitoringRecord.patient_id == RenalPatient.id)
                .filter(
                    RenalPatient.patient_code == patient_code,
                    MonitoringRecord.record_type == 'MORNING',
                    MonitoringRecord.recorded_at == recorded_at,
                )
                .first()
            )
            if exists is not None:
                continue

            weight_kg = round(dry_weight_kg + (0.6 + (6 - day_offset) * 0.22), 1)
            systolic = 146 + (6 - day_offset) * 5
            if is_primary_patient and day_offset == 0:
                systolic = 176
            payload = {
                'patient_id': patient_code,
                'record_type': 'MORNING',
                'recorded_at': recorded_at.isoformat(),
                'submitted_by_role': 'PATIENT',
                'measurements': {
                    'weight_kg': weight_kg,
                    'systolic': systolic,
                    'diastolic': 92 if is_primary_patient else 86,
                    'pulse': 88 if is_primary_patient else 76,
                },
                'symptoms': {
                    'fall': bool(is_primary_patient and day_offset == 1),
                },
                'confirmed': True,
            }
            if is_diabetic:
                payload['measurements']['blood_glucose_mg_dl'] = 168 if is_primary_patient else 142
            renal_monitoring_service.create_monitoring_record(db=db, payload=payload)
            created_count += 1
        return created_count

    # 目的：補齊今日透析 session、事件與洗後確認。
    # 為什麼：護理師儀表板的「今日透析」與病患頁摘要需完整流程資料。
    def _seed_today_dialysis_session(
        self,
        *,
        db: Session,
        patient_code: str,
        dialysis_date: date,
        bed_no: str,
        machine_no: str,
        dry_weight_kg: float,
        is_primary_patient: bool,
    ) -> dict:
        created_sessions = 0
        created_events = 0
        post_checked_sessions = 0
        patient_row = db.query(RenalPatient).filter(RenalPatient.patient_code == patient_code).first()
        if patient_row is None:
            return {
                'created_sessions': created_sessions,
                'created_events': created_events,
                'post_checked_sessions': post_checked_sessions,
            }

        session_row = (
            db.query(DialysisSession)
            .filter(DialysisSession.patient_id == patient_row.id, DialysisSession.dialysis_date == dialysis_date)
            .order_by(DialysisSession.created_at.desc())
            .first()
        )

        if session_row is None:
            create_response = renal_dialysis_service.create_session(
                db=db,
                payload={
                    'patient_id': patient_code,
                    'dialysis_date': dialysis_date.isoformat(),
                    'shift': 'MORNING',
                    'bed_no': bed_no,
                    'machine_no': machine_no,
                    'created_by': 'N001',
                },
            )
            created_sessions += 1
            session_id = str(create_response['session_id'])
            session_row = db.query(DialysisSession).filter(DialysisSession.id == session_id).first()

        if session_row is not None and session_row.pre_weight_kg is None:
            renal_dialysis_service.update_pre_check(
                db=db,
                session_id=str(session_row.id),
                payload={
                    'pre_weight_kg': round(dry_weight_kg + (3.4 if is_primary_patient else 1.2), 1),
                    'dry_weight_kg': dry_weight_kg,
                    'target_uf_l': 3.0 if is_primary_patient else 1.4,
                    'confirmed_by': 'N001',
                    'note': 'demo pre-check',
                },
            )

        if session_row is not None and is_primary_patient:
            created_events += self._create_event_once(
                db=db,
                session_id=str(session_row.id),
                event_type='BP_LOW',
                payload={
                    'event_at': datetime.combine(dialysis_date, time(hour=8, minute=30)).isoformat(),
                    'payload': {'bp': '84/52', 'symptom': 'dizziness'},
                    'handled_by': 'N001',
                    'action': '暫停脫水後恢復',
                },
            )

        if session_row is not None and str(session_row.completion_status) not in {'COMPLETED', 'ENDED_EARLY'}:
            renal_dialysis_service.post_check(
                db=db,
                payload={
                    'session_id': str(session_row.id),
                    'post_weight_kg': round(dry_weight_kg + (1.2 if is_primary_patient else 0.0), 1),
                    'actual_uf_l': 2.2 if is_primary_patient else 1.4,
                    'completion_status': 'COMPLETED',
                    'has_tourniquet': False,
                    'confirmed_by': 'N001',
                },
            )
            post_checked_sessions += 1

        return {
            'created_sessions': created_sessions,
            'created_events': created_events,
            'post_checked_sessions': post_checked_sessions,
        }

    def _create_event_once(self, *, db: Session, session_id: str, event_type: str, payload: dict) -> int:
        event_at_value = str(payload.get('event_at') or '').strip()
        exists = (
            db.query(DialysisEvent)
            .filter(
                DialysisEvent.session_id == session_id,
                DialysisEvent.event_type == event_type,
                DialysisEvent.event_at == datetime.fromisoformat(event_at_value),
            )
            .first()
        )
        if exists is not None:
            return 0
        renal_dialysis_service.create_event(
            db=db,
            payload={
                'session_id': session_id,
                'event_type': event_type,
                'event_at': event_at_value,
                'payload': payload.get('payload') or {},
                'handled_by': payload.get('handled_by'),
                'action': payload.get('action'),
            },
        )
        return 1


renal_demo_data_service = RenalDemoDataService()
