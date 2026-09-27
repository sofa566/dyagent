from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from src.api.errors import not_found_error, validation_error
from src.models import DialysisEvent, DialysisSession, FollowUpCase, RenalPatient


class RenalDialysisService:
    # 目的：封裝 S02 透析療程建立、事件記錄與洗後確認流程。
    # 為什麼：將狀態轉移規則集中管理，避免路由散落條件判斷。

    def create_session(self, *, db: Session, payload: dict) -> dict:
        patient = self._get_patient_by_code(db=db, patient_code=str((payload or {}).get('patient_id') or '').strip())
        dialysis_date = self._parse_date(str((payload or {}).get('dialysis_date') or '').strip())
        shift = str((payload or {}).get('shift') or '').strip().upper()
        bed_no = str((payload or {}).get('bed_no') or '').strip()
        machine_no = str((payload or {}).get('machine_no') or '').strip()
        created_by = str((payload or {}).get('created_by') or '').strip()
        self._validate_session_fields(shift=shift, bed_no=bed_no, machine_no=machine_no)

        row = DialysisSession(
            patient_id=patient.id,
            dialysis_date=dialysis_date,
            shift=shift,
            bed_no=bed_no,
            machine_no=machine_no,
            created_by=created_by,
            completion_status='CREATED',
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'session_id': str(row.id), 'status': str(row.completion_status)}

    def update_pre_check(self, *, db: Session, session_id: str, payload: dict) -> dict:
        # 目的：寫入洗前確認資料並更新狀態。
        # 為什麼：透析療程需先完成洗前確認，才能進入洗中與洗後流程。
        session = self._get_session(db=db, session_id=session_id)
        pre_weight_kg = payload.get('pre_weight_kg')
        dry_weight_kg = payload.get('dry_weight_kg')
        target_uf_l = payload.get('target_uf_l')
        confirmed_by = str((payload or {}).get('confirmed_by') or '').strip()
        if pre_weight_kg is None or dry_weight_kg is None or target_uf_l is None:
            raise validation_error('pre_weight_kg / dry_weight_kg / target_uf_l 不可為空')
        session.pre_weight_kg = pre_weight_kg
        session.dry_weight_kg = dry_weight_kg
        session.target_uf_l = target_uf_l
        session.note = str((payload or {}).get('note') or '').strip() or None
        session.confirmed_by = confirmed_by or None
        session.completion_status = 'PRE_CHECK_CONFIRMED'
        db.add(session)
        db.commit()
        db.refresh(session)
        return {'ok': True, 'session_id': str(session.id), 'status': str(session.completion_status)}

    def create_event(self, *, db: Session, payload: dict) -> dict:
        # 目的：寫入透析中事件並視需要建立追蹤案件。
        # 為什麼：異常事件需有可追溯處置紀錄，供護理工作台與結案流程使用。
        session = self._get_session(db=db, session_id=str((payload or {}).get('session_id') or '').strip())
        event_type = str((payload or {}).get('event_type') or '').strip().upper()
        event_at = self._parse_datetime(str((payload or {}).get('event_at') or '').strip())
        payload_obj = payload.get('payload') if isinstance(payload.get('payload'), dict) else {}
        handled_by = str((payload or {}).get('handled_by') or '').strip() or None
        action = str((payload or {}).get('action') or '').strip() or None
        if not event_type:
            raise validation_error('event_type 不可為空')

        follow_up_required = event_type in {'BP_HIGH', 'BP_LOW', 'CRAMP', 'EARLY_END'}
        event = DialysisEvent(
            session_id=session.id,
            event_type=event_type,
            event_at=event_at,
            payload=payload_obj,
            handled_by=handled_by,
            action=action,
            follow_up_required=follow_up_required,
        )
        db.add(event)
        db.flush()

        if follow_up_required:
            follow_up = FollowUpCase(
                patient_id=session.patient_id,
                source_type='dialysis',
                source_record_id=str(event.id),
                severity='warning',
                status='OPEN',
                title=f'透析事件追蹤：{event_type}',
                details={'event_type': event_type, 'action': action, 'payload': payload_obj},
            )
            db.add(follow_up)

        session.completion_status = 'IN_PROGRESS'
        db.add(session)
        db.commit()
        db.refresh(event)
        return {'ok': True, 'event_id': str(event.id), 'follow_up_required': follow_up_required}

    def post_check(self, *, db: Session, payload: dict) -> dict:
        # 目的：寫入洗後確認並關閉或標記療程狀態。
        # 為什麼：洗後資料是單次透析結束依據，需要明確狀態轉移與提醒資料。
        session = self._get_session(db=db, session_id=str((payload or {}).get('session_id') or '').strip())
        if str(session.completion_status) not in {'PRE_CHECK_CONFIRMED', 'IN_PROGRESS', 'CREATED'}:
            raise validation_error('目前狀態不可進行洗後確認')

        post_weight_kg = payload.get('post_weight_kg')
        actual_uf_l = payload.get('actual_uf_l')
        completion_status = str((payload or {}).get('completion_status') or '').strip().upper()
        if completion_status not in {'COMPLETED', 'ENDED_EARLY'}:
            raise validation_error('completion_status 僅支援 COMPLETED 或 ENDED_EARLY')
        if post_weight_kg is None or actual_uf_l is None:
            raise validation_error('post_weight_kg / actual_uf_l 不可為空')

        session.post_weight_kg = post_weight_kg
        session.actual_uf_l = actual_uf_l
        session.completion_status = completion_status
        session.has_tourniquet = bool((payload or {}).get('has_tourniquet'))
        session.confirmed_by = str((payload or {}).get('confirmed_by') or '').strip() or session.confirmed_by
        db.add(session)
        db.commit()
        db.refresh(session)

        return {
            'ok': True,
            'session_id': str(session.id),
            'status': str(session.completion_status),
            'post_check_reminder': {
                'required': bool(session.has_tourniquet),
                'after_minutes': 60,
                'message': '請確認止血帶已解除。' if session.has_tourniquet else '',
            },
        }

    def _get_patient_by_code(self, *, db: Session, patient_code: str) -> RenalPatient:
        if not patient_code:
            raise validation_error('patient_id 不可為空')
        row = db.query(RenalPatient).filter(RenalPatient.patient_code == patient_code, RenalPatient.enabled == True).first()  # noqa: E712
        if row is None:
            raise not_found_error('RenalPatient', patient_code)
        return row

    def _validate_session_fields(self, *, shift: str, bed_no: str, machine_no: str) -> None:
        if shift not in {'MORNING', 'AFTERNOON', 'EVENING'}:
            raise validation_error('shift 無效')
        if not bed_no:
            raise validation_error('bed_no 不可為空')
        if not machine_no:
            raise validation_error('machine_no 不可為空')

    def _parse_date(self, value: str) -> date:
        if not value:
            raise validation_error('dialysis_date 不可為空')
        try:
            return date.fromisoformat(value)
        except Exception as error:
            raise validation_error('dialysis_date 格式錯誤，需為 YYYY-MM-DD') from error

    def _parse_datetime(self, value: str) -> datetime:
        if not value:
            raise validation_error('event_at 不可為空')
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except Exception as error:
            raise validation_error('event_at 格式錯誤，需為 ISO 8601') from error

    def _get_session(self, *, db: Session, session_id: str) -> DialysisSession:
        if not session_id:
            raise validation_error('session_id 不可為空')
        row = db.query(DialysisSession).filter(DialysisSession.id == session_id).first()
        if row is None:
            raise not_found_error('DialysisSession', session_id)
        return row


renal_dialysis_service = RenalDialysisService()
