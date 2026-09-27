from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from src.api.errors import validation_error
from src.core.logging import get_logger
from src.models import LineChannelSession, LineMessage, MonitoringRecord, RenalPatient
from src.services.redis_service import redis_service
from src.services.renal_monitoring_service import renal_monitoring_service

logger = get_logger(__name__)

BACKFILL_RUNS_CACHE_KEY = 'line:monitoring:backfill:runs'
BACKFILL_RUNS_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
BACKFILL_RUNS_CACHE_MAX_COUNT = 30
BACKFILL_MAX_DATE_SPAN_DAYS = 31


class MonitoringBackfillService:
    # 目的：回補 LINE 對話中的歷史監測資料到 monitoring_records。
    # 為什麼：修復早期解析遺漏造成的「有回覆但未入庫」缺口，維持照護資料完整性。

    def __init__(self) -> None:
        self._memory_recent_runs: list[dict] = []

    async def run_backfill(
        self,
        *,
        db: Session,
        start_date: date,
        end_date: date,
        triggered_by: str,
        max_scan_rows: int,
    ) -> dict:
        # 目的：執行指定日期區間的歷史補寫。
        # 為什麼：讓營運可手動補帳與排程補帳共用同一套回補邏輯。
        safe_start_date, safe_end_date = self._normalize_date_range(start_date=start_date, end_date=end_date)
        safe_scan_rows = max(100, min(int(max_scan_rows or 5000), 30000))
        scan_start_at = datetime.combine(safe_start_date, datetime.min.time())
        scan_end_at = datetime.combine(safe_end_date + timedelta(days=1), datetime.min.time())

        inbound_messages = self._load_inbound_messages(
            db=db,
            start_at=scan_start_at,
            end_at=scan_end_at,
            limit=safe_scan_rows,
        )
        session_cache: dict[str, LineChannelSession | None] = {}
        patient_cache_by_session_id: dict[str, RenalPatient | None] = {}
        draft_measurements_by_key: dict[str, dict] = {}

        parsed_count = 0
        created_count = 0
        skipped_existing_count = 0
        failed_count = 0

        for inbound_message in inbound_messages:
            session = self._resolve_session(
                db=db,
                message=inbound_message,
                session_cache=session_cache,
            )
            if session is None:
                continue
            patient = self._resolve_patient(
                db=db,
                session=session,
                patient_cache_by_session_id=patient_cache_by_session_id,
            )
            if patient is None:
                continue

            incoming_measurements = self._build_measurements_from_text(str(inbound_message.content or ''))
            if not incoming_measurements:
                continue
            parsed_count += 1

            record_type = self._resolve_record_type(now=inbound_message.created_at or datetime.now())
            record_date = (inbound_message.created_at or datetime.now()).date().isoformat()
            draft_key = self._build_draft_key(patient=patient, record_date=record_date, record_type=record_type)
            merged_measurements = self._merge_measurements(
                draft_measurements=draft_measurements_by_key.get(draft_key) or {},
                incoming_measurements=incoming_measurements,
            )

            if self._has_missing_required_measurements(patient=patient, measurements=merged_measurements):
                draft_measurements_by_key[draft_key] = merged_measurements
                continue

            if self._has_existing_record(
                db=db,
                patient=patient,
                record_date=datetime.fromisoformat(record_date).date(),
                record_type=record_type,
            ):
                skipped_existing_count += 1
                draft_measurements_by_key.pop(draft_key, None)
                continue

            if self._try_create_record(
                db=db,
                patient=patient,
                record_type=record_type,
                recorded_at=inbound_message.created_at or datetime.now(),
                measurements=merged_measurements,
            ):
                created_count += 1
                draft_measurements_by_key.pop(draft_key, None)
                continue
            failed_count += 1

        summary = {
            'run_id': str(uuid.uuid4()),
            'triggered_by': str(triggered_by or 'manual').strip() or 'manual',
            'date_range': {'start_date': safe_start_date.isoformat(), 'end_date': safe_end_date.isoformat()},
            'max_scan_rows': safe_scan_rows,
            'scanned_messages': len(inbound_messages),
            'parsed_messages': parsed_count,
            'created_records': created_count,
            'skipped_existing_records': skipped_existing_count,
            'failed_records': failed_count,
            'pending_partial_groups': len(draft_measurements_by_key),
            'executed_at': datetime.now().isoformat(),
        }
        await self._append_run_summary(summary=summary)
        return summary

    async def list_recent_runs(self, *, limit: int) -> dict:
        # 目的：回傳最近補寫任務的執行摘要。
        # 為什麼：讓護理與營運能快速查核補帳是否有跑完且成效正常。
        safe_limit = max(1, min(int(limit or 10), 100))
        if redis_service.client is not None:
            cache_payload = await redis_service.get_json(BACKFILL_RUNS_CACHE_KEY)
            if isinstance(cache_payload, list):
                return {'runs': cache_payload[:safe_limit]}
        return {'runs': self._memory_recent_runs[:safe_limit]}

    def _normalize_date_range(self, *, start_date: date, end_date: date) -> tuple[date, date]:
        if start_date > end_date:
            raise validation_error('start_date 不可晚於 end_date')
        span_days = (end_date - start_date).days
        if span_days > BACKFILL_MAX_DATE_SPAN_DAYS:
            raise validation_error(f'日期區間不可超過 {BACKFILL_MAX_DATE_SPAN_DAYS + 1} 天')
        return start_date, end_date

    def _load_inbound_messages(self, *, db: Session, start_at: datetime, end_at: datetime, limit: int) -> list[LineMessage]:
        # 目的：載入候選的 LINE inbound 訊息供補寫掃描。
        # 為什麼：回補以使用者原始回報為準，避免將系統或 agent 回覆誤當量測來源。
        return (
            db.query(LineMessage)
            .filter(
                LineMessage.direction == 'inbound',
                LineMessage.sender_type == 'user',
                LineMessage.created_at >= start_at,
                LineMessage.created_at < end_at,
            )
            .order_by(LineMessage.created_at.asc())
            .limit(limit)
            .all()
        )

    def _resolve_session(
        self,
        *,
        db: Session,
        message: LineMessage,
        session_cache: dict[str, LineChannelSession | None],
    ) -> LineChannelSession | None:
        session_id = str(message.session_id or '')
        if not session_id:
            return None
        if session_id in session_cache:
            return session_cache[session_id]
        found_session = db.query(LineChannelSession).filter(LineChannelSession.id == message.session_id).first()
        session_cache[session_id] = found_session
        return found_session

    def _resolve_patient(
        self,
        *,
        db: Session,
        session: LineChannelSession,
        patient_cache_by_session_id: dict[str, RenalPatient | None],
    ) -> RenalPatient | None:
        session_id = str(session.id)
        if session_id in patient_cache_by_session_id:
            return patient_cache_by_session_id[session_id]

        found_patient: RenalPatient | None = None
        if session.bound_patient_id:
            found_patient = (
                db.query(RenalPatient)
                .filter(RenalPatient.id == session.bound_patient_id, RenalPatient.enabled == True)  # noqa: E712
                .first()
            )
        if found_patient is None:
            normalized_line_user_id = str(session.line_user_id or '').strip()
            if normalized_line_user_id:
                found_patient = (
                    db.query(RenalPatient)
                    .filter(RenalPatient.line_user_id == normalized_line_user_id, RenalPatient.enabled == True)  # noqa: E712
                    .first()
                )
        patient_cache_by_session_id[session_id] = found_patient
        return found_patient

    def _parse_blood_pressure(self, *, text: str) -> tuple[int, int] | None:
        matched = re.search(r'(\d{2,3})\s*/\s*(\d{2,3})', text)
        if matched is None:
            return None
        return int(matched.group(1)), int(matched.group(2))

    def _parse_weight_kg(self, *, text: str) -> float | None:
        normalized_text = str(text or '').strip().lower()
        if not normalized_text:
            return None
        keyword_patterns = [
            r'體重\s*[:：]?\s*(\d{2,3}(?:\.\d{1,2})?)',
            r'wt\s*[:：]?\s*(\d{2,3}(?:\.\d{1,2})?)',
            r'weight\s*[:：]?\s*(\d{2,3}(?:\.\d{1,2})?)',
        ]
        for pattern in keyword_patterns:
            matched = re.search(pattern, normalized_text)
            if matched is not None:
                return float(matched.group(1))

        unit_patterns = [
            r'(\d{2,3}(?:\.\d{1,2})?)\s*kg',
            r'(\d{2,3}(?:\.\d{1,2})?)\s*公斤',
        ]
        for pattern in unit_patterns:
            matched = re.search(pattern, normalized_text)
            if matched is not None:
                return float(matched.group(1))

        blood_pressure_match = re.search(r'\d{2,3}\s*/\s*\d{2,3}', normalized_text)
        text_without_bp = normalized_text
        if blood_pressure_match is not None:
            start, end = blood_pressure_match.span()
            text_without_bp = f'{normalized_text[:start]} {normalized_text[end:]}'
        for matched in re.finditer(r'(?<!\d)(\d{2,3}(?:\.\d{1,2})?)(?!\d)', text_without_bp):
            matched_value = float(matched.group(1))
            if matched_value < 20 or matched_value > 250:
                continue
            nearby_text = text_without_bp[max(0, matched.start() - 6): matched.end() + 6]
            if '血糖' in nearby_text or 'glucose' in nearby_text:
                continue
            return matched_value
        return None

    def _parse_blood_glucose(self, *, text: str) -> int | None:
        normalized_text = str(text or '').strip().lower()
        for pattern in (r'血糖\s*[:：]?\s*(\d{2,3})', r'glucose\s*[:：]?\s*(\d{2,3})'):
            matched = re.search(pattern, normalized_text)
            if matched is not None:
                return int(matched.group(1))
        return None

    def _build_measurements_from_text(self, text: str) -> dict:
        measurements: dict[str, int | float] = {}
        blood_pressure = self._parse_blood_pressure(text=text)
        if blood_pressure is not None:
            measurements['systolic'], measurements['diastolic'] = blood_pressure
        weight_kg = self._parse_weight_kg(text=text)
        if weight_kg is not None:
            measurements['weight_kg'] = weight_kg
        glucose_value = self._parse_blood_glucose(text=text)
        if glucose_value is not None:
            measurements['blood_glucose_mg_dl'] = glucose_value
        return measurements

    def _resolve_record_type(self, *, now: datetime) -> str:
        return 'MORNING' if int(now.hour) < 15 else 'EVENING'

    def _build_draft_key(self, *, patient: RenalPatient, record_date: str, record_type: str) -> str:
        return f'{patient.id}:{record_date}:{record_type}'

    def _merge_measurements(self, *, draft_measurements: dict, incoming_measurements: dict) -> dict:
        merged = dict(draft_measurements or {})
        for field_name in ('systolic', 'diastolic', 'weight_kg', 'blood_glucose_mg_dl'):
            if incoming_measurements.get(field_name) is not None:
                merged[field_name] = incoming_measurements[field_name]
        return merged

    def _has_missing_required_measurements(self, *, patient: RenalPatient, measurements: dict) -> bool:
        has_bp = measurements.get('systolic') is not None and measurements.get('diastolic') is not None
        has_weight = measurements.get('weight_kg') is not None
        if not has_bp or not has_weight:
            return True
        is_diabetic = bool(getattr(patient, 'is_diabetic', False))
        if is_diabetic and measurements.get('blood_glucose_mg_dl') is None:
            return True
        return False

    def _has_existing_record(self, *, db: Session, patient: RenalPatient, record_date: date, record_type: str) -> bool:
        # 目的：判斷同病患同日同時段是否已有正式紀錄。
        # 為什麼：回補必須保守避免重複入庫，優先維持既有資料穩定。
        day_start = datetime.combine(record_date, datetime.min.time())
        day_end = day_start + timedelta(days=1)
        found_record = (
            db.query(MonitoringRecord)
            .filter(
                MonitoringRecord.patient_id == patient.id,
                MonitoringRecord.record_type == record_type,
                MonitoringRecord.submitted_by_role == 'PATIENT',
                MonitoringRecord.recorded_at >= day_start,
                MonitoringRecord.recorded_at < day_end,
            )
            .first()
        )
        return found_record is not None

    def _try_create_record(
        self,
        *,
        db: Session,
        patient: RenalPatient,
        record_type: str,
        recorded_at: datetime,
        measurements: dict,
    ) -> bool:
        # 目的：嘗試建立單筆 monitoring_record。
        # 為什麼：讓回補流程可統一呼叫現有醫療規則驗證與追蹤案件建立邏輯。
        try:
            renal_monitoring_service.create_monitoring_record(
                db=db,
                payload={
                    'patient_id': str(patient.patient_code or ''),
                    'record_type': record_type,
                    'recorded_at': recorded_at.isoformat(),
                    'submitted_by_role': 'PATIENT',
                    'measurements': measurements,
                    'symptoms': {},
                    'confirmed': True,
                },
            )
            return True
        except Exception as error:
            logger.warning(
                'monitoring_backfill_create_failed',
                patient_id=str(patient.patient_code or ''),
                record_type=record_type,
                error=str(error)[:300],
            )
            return False

    async def _append_run_summary(self, *, summary: dict) -> None:
        rows = [summary, *self._memory_recent_runs]
        self._memory_recent_runs = rows[:BACKFILL_RUNS_CACHE_MAX_COUNT]
        if redis_service.client is None:
            return
        existing_rows = await redis_service.get_json(BACKFILL_RUNS_CACHE_KEY)
        if not isinstance(existing_rows, list):
            existing_rows = []
        merged_rows = [summary, *existing_rows]
        await redis_service.set_json(
            BACKFILL_RUNS_CACHE_KEY,
            merged_rows[:BACKFILL_RUNS_CACHE_MAX_COUNT],
            expire=BACKFILL_RUNS_CACHE_TTL_SECONDS,
        )


monitoring_backfill_service = MonitoringBackfillService()
