from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from src.api.errors import not_found_error, validation_error
from src.models import FollowUpCase, MonitoringRecord, RenalPatient

HIGH_SYSTOLIC_THRESHOLD = 180
LOW_SYSTOLIC_THRESHOLD = 90
WEIGHT_DELTA_THRESHOLD_KG = Decimal('2.0')


class RenalMonitoringService:
    # 目的：封裝 S01 日常監測寫入、比對與追蹤判定流程。
    # 為什麼：將路由與業務規則解耦，避免 API 層混入大量醫療場景條件。

    def create_monitoring_record(self, *, db: Session, payload: dict) -> dict:
        # 目的：建立日常監測紀錄並回傳比對與追蹤結果。
        # 為什麼：前端/LINE 需一次取得可呈現結果，避免多次往返查詢。
        patient = self._get_patient_by_code(db=db, patient_code=str((payload or {}).get('patient_id') or '').strip())
        record_type = str((payload or {}).get('record_type') or '').strip().upper()
        submitted_by_role = str((payload or {}).get('submitted_by_role') or '').strip().upper()
        recorded_at = self._parse_datetime(str((payload or {}).get('recorded_at') or '').strip())
        measurements = payload.get('measurements') if isinstance(payload.get('measurements'), dict) else {}
        symptoms = payload.get('symptoms') if isinstance(payload.get('symptoms'), dict) else {}
        confirmed = bool((payload or {}).get('confirmed'))
        self._validate_required_fields(
            patient=patient,
            record_type=record_type,
            submitted_by_role=submitted_by_role,
            measurements=measurements,
            confirmed=confirmed,
        )

        comparison_result = self._build_comparison_result(patient=patient, measurements=measurements)
        matched_rules = self._detect_rules(comparison_result=comparison_result, measurements=measurements, symptoms=symptoms)
        follow_up_required = bool(matched_rules)
        patient_message = self._build_patient_message(follow_up_required=follow_up_required)

        row = MonitoringRecord(
            patient_id=patient.id,
            record_type=record_type,
            recorded_at=recorded_at,
            submitted_by_role=submitted_by_role,
            measurements=measurements,
            symptoms=symptoms,
            confirmed=confirmed,
            record_status='SAVED',
            comparison_result=comparison_result,
            matched_rules=matched_rules,
            follow_up_required=follow_up_required,
            patient_message=patient_message,
        )
        db.add(row)
        db.flush()

        follow_up_payload = {'required': False, 'case_id': None, 'assigned_nurse_id': None, 'status': None}
        if follow_up_required:
            follow_up_row = FollowUpCase(
                patient_id=patient.id,
                source_type='monitoring',
                source_record_id=str(row.id),
                severity=self._pick_follow_up_severity(matched_rules=matched_rules),
                status='OPEN',
                title='日常監測需追蹤',
                details={'matched_rules': matched_rules},
                assigned_nurse_id=str(patient.primary_nurse_id or ''),
            )
            db.add(follow_up_row)
            db.flush()
            follow_up_payload = {
                'required': True,
                'case_id': str(follow_up_row.id),
                'assigned_nurse_id': follow_up_row.assigned_nurse_id,
                'status': str(follow_up_row.status),
            }

        db.commit()
        db.refresh(row)
        return {
            'ok': True,
            'monitoring_record_id': str(row.id),
            'record_status': str(row.record_status),
            'comparison_result': comparison_result,
            'matched_rules': matched_rules,
            'follow_up': follow_up_payload,
            'patient_message': patient_message,
        }

    def list_monitoring_records(self, *, db: Session, patient_code: str, days: int) -> dict:
        patient = self._get_patient_by_code(db=db, patient_code=patient_code)
        safe_days = max(1, min(int(days or 7), 30))
        time_lower_bound = datetime.now().timestamp() - safe_days * 86400
        rows = (
            db.query(MonitoringRecord)
            .filter(MonitoringRecord.patient_id == patient.id, MonitoringRecord.recorded_at >= datetime.fromtimestamp(time_lower_bound))
            .order_by(MonitoringRecord.recorded_at.desc())
            .all()
        )
        return {
            'ok': True,
            'patient_id': str(patient.patient_code),
            'days': safe_days,
            'records': [
                {
                    'id': str(row.id),
                    'record_type': str(row.record_type),
                    'recorded_at': row.recorded_at.isoformat() if row.recorded_at else None,
                    'weight_kg': (row.measurements or {}).get('weight_kg'),
                    'bp': self._format_bp(measurements=row.measurements or {}),
                    'follow_up_required': bool(row.follow_up_required),
                }
                for row in rows
            ],
        }

    def _get_patient_by_code(self, *, db: Session, patient_code: str) -> RenalPatient:
        if not patient_code:
            raise validation_error('patient_id 不可為空')
        row = db.query(RenalPatient).filter(RenalPatient.patient_code == patient_code, RenalPatient.enabled == True).first()  # noqa: E712
        if row is None:
            raise not_found_error('RenalPatient', patient_code)
        return row

    def _validate_required_fields(
        self,
        *,
        patient: RenalPatient,
        record_type: str,
        submitted_by_role: str,
        measurements: dict,
        confirmed: bool,
    ) -> None:
        allowed_record_types = {'MORNING', 'EVENING', 'SYMPTOM_REPORT'}
        allowed_submitter_roles = {'PATIENT', 'FAMILY', 'CAREGIVER', 'NURSE'}
        if record_type not in allowed_record_types:
            raise validation_error('record_type 無效')
        if submitted_by_role not in allowed_submitter_roles:
            raise validation_error('submitted_by_role 無效')
        if not confirmed:
            raise validation_error('confirmed 必須為 true 才可送出')
        if not isinstance(measurements, dict) or not measurements:
            raise validation_error('measurements 不可為空')
        if record_type in {'MORNING', 'EVENING'}:
            self._validate_routine_measurements(patient=patient, measurements=measurements)

    def _validate_routine_measurements(self, *, patient: RenalPatient, measurements: dict) -> None:
        required_bp_fields = ['systolic', 'diastolic']
        for field_name in required_bp_fields:
            if measurements.get(field_name) is None:
                raise validation_error(f'{field_name} 不可為空')
        if measurements.get('weight_kg') is None:
            raise validation_error('weight_kg 不可為空')
        if bool(getattr(patient, 'is_diabetic', False)) and measurements.get('blood_glucose_mg_dl') is None:
            raise validation_error('糖尿病腎友需填寫 blood_glucose_mg_dl')

    def _parse_datetime(self, value: str) -> datetime:
        if not value:
            raise validation_error('recorded_at 不可為空')
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except Exception as error:
            raise validation_error('recorded_at 格式錯誤，需為 ISO 8601') from error

    def _build_comparison_result(self, *, patient: RenalPatient, measurements: dict) -> dict:
        dry_weight = Decimal(str(getattr(patient, 'dry_weight_kg', None) or '0'))
        current_weight = Decimal(str(measurements.get('weight_kg') or '0'))
        if dry_weight <= 0 or current_weight <= 0:
            return {
                'dry_weight_kg': float(dry_weight),
                'current_weight_kg': float(current_weight),
                'weight_delta_kg': None,
                'weight_delta_percent': None,
            }
        delta = current_weight - dry_weight
        percent = (delta / dry_weight) * Decimal('100')
        return {
            'dry_weight_kg': float(dry_weight),
            'current_weight_kg': float(current_weight),
            'weight_delta_kg': float(delta),
            'weight_delta_percent': float(percent.quantize(Decimal('0.1'))),
        }

    def _detect_rules(self, *, comparison_result: dict, measurements: dict, symptoms: dict) -> list[dict]:
        matched_rules: list[dict] = []
        systolic = int(measurements.get('systolic') or 0)
        if systolic >= HIGH_SYSTOLIC_THRESHOLD:
            matched_rules.append({'rule_id': 'BP_HIGH', 'rule_version': 'v1', 'severity': 'warning', 'message': '收縮壓偏高，需追蹤。'})
        if systolic and systolic <= LOW_SYSTOLIC_THRESHOLD:
            matched_rules.append({'rule_id': 'BP_LOW', 'rule_version': 'v1', 'severity': 'warning', 'message': '收縮壓偏低，需追蹤。'})

        weight_delta = comparison_result.get('weight_delta_kg')
        if weight_delta is not None and Decimal(str(weight_delta)) >= WEIGHT_DELTA_THRESHOLD_KG:
            matched_rules.append({'rule_id': 'WEIGHT_GAIN', 'rule_version': 'v1', 'severity': 'warning', 'message': '體重增加超過門檻，需追蹤。'})

        if bool((symptoms or {}).get('fall')):
            matched_rules.append({'rule_id': 'SYMPTOM_FALL', 'rule_version': 'v1', 'severity': 'critical', 'message': '通報摔倒事件，請優先處理。'})
        if bool((symptoms or {}).get('breathing_discomfort')):
            matched_rules.append({'rule_id': 'SYMPTOM_BREATHING', 'rule_version': 'v1', 'severity': 'critical', 'message': '出現呼吸不適，請優先處理。'})
        return matched_rules

    def _pick_follow_up_severity(self, *, matched_rules: list[dict]) -> str:
        for row in matched_rules:
            if str((row or {}).get('severity') or '') == 'critical':
                return 'critical'
        return 'warning'

    def _build_patient_message(self, *, follow_up_required: bool) -> str:
        if follow_up_required:
            return '已收到您的填報，主護理師將持續追蹤。'
        return '已收到您的填報，目前數據在觀察範圍內。'

    def _format_bp(self, *, measurements: dict) -> str:
        systolic = measurements.get('systolic')
        diastolic = measurements.get('diastolic')
        if systolic is None or diastolic is None:
            return ''
        return f'{systolic}/{diastolic}'


renal_monitoring_service = RenalMonitoringService()
