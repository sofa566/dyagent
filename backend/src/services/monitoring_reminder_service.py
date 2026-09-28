from __future__ import annotations

from datetime import date, datetime, time

import httpx
from sqlalchemy.orm import Session

from src.api.errors import validation_error
from src.core.config import settings
from src.core.logging import get_logger
from src.models import (
    LineChannelSession,
    LineMessage,
    Message,
    MonitoringRecord,
    MonitoringReminderDeliveryLog,
    MonitoringReminderJob,
    MonitoringReminderPolicy,
    RenalPatient,
)
from src.services.line_onboarding_service import line_onboarding_service

logger = get_logger(__name__)


class MonitoringReminderService:
    # 目的：集中處理腎友每日回報完整性檢核與提醒派送。
    # 為什麼：讓提醒規則與投遞稽核有單一服務層，避免散落在 route 或前端。

    def get_daily_compliance(self, *, db: Session, target_date: date) -> dict:
        # 目的：回傳指定日期的回報完整性。
        # 為什麼：護理端與排程都需要同一份可判斷「缺早/缺晚/缺血糖」的資料來源。
        patient_rows = db.query(RenalPatient).filter(RenalPatient.enabled == True).all()  # noqa: E712
        active_binding_map = self._build_active_line_binding_map(db=db)
        patient_items = [
            self._build_patient_compliance_item(
                db=db,
                patient=patient,
                target_date=target_date,
                active_binding_map=active_binding_map,
            )
            for patient in patient_rows
        ]
        compliant_count = sum(1 for item in patient_items if bool(item.get('compliant')))
        return {
            'ok': True,
            'date': target_date.isoformat(),
            'summary': {
                'total_patients': len(patient_items),
                'compliant_patients': compliant_count,
                'non_compliant_patients': len(patient_items) - compliant_count,
            },
            'patients': patient_items,
        }

    def list_policies(self, *, db: Session) -> dict:
        # 目的：回傳提醒 policy 清單與目前排程設定。
        # 為什麼：後台 UI 需要同一 API 同步顯示模板與時段設定，降低操作成本。
        policy_rows = []
        for window in ('MORNING', 'EVENING'):
            policy_rows.append(self._get_or_create_default_policy(db=db, window=window))
        db.commit()
        return {
            'ok': True,
            'schedule': {
                'morning_dispatch_time': str(getattr(settings, 'MONITORING_REMINDER_MORNING_DISPATCH_TIME', '06:00') or '06:00'),
                'evening_dispatch_time': str(getattr(settings, 'MONITORING_REMINDER_EVENING_DISPATCH_TIME', '21:00') or '21:00'),
                'backfill_first_run_time': str(getattr(settings, 'MONITORING_BACKFILL_FIRST_RUN_TIME', '00:10') or '00:10'),
                'backfill_second_run_time': str(getattr(settings, 'MONITORING_BACKFILL_SECOND_RUN_TIME', '12:10') or '12:10'),
            },
            'policies': [self._serialize_policy(policy=policy_row) for policy_row in policy_rows],
        }

    def update_policy(self, *, db: Session, policy_id: str, payload: dict) -> dict:
        # 目的：更新提醒 policy（啟用、模板、糖友血糖需求）。
        # 為什麼：護理/營運需在 UI 即時調整話術與啟用狀態，而不需改程式或手打 SQL。
        row = db.query(MonitoringReminderPolicy).filter(MonitoringReminderPolicy.id == policy_id).first()
        if row is None:
            raise validation_error('找不到指定的提醒 policy')

        if 'enabled' in payload:
            row.enabled = bool(payload.get('enabled'))
        if 'requires_glucose_for_diabetic' in payload:
            row.requires_glucose_for_diabetic = bool(payload.get('requires_glucose_for_diabetic'))
        if 'message_template' in payload:
            message_template = str(payload.get('message_template') or '').strip()
            if not message_template:
                raise validation_error('message_template 不可為空')
            row.message_template = message_template
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'policy': self._serialize_policy(policy=row)}

    def get_dispatch_report(self, *, db: Session, target_date: date, window: str, status: str, limit: int) -> dict:
        # 目的：查詢指定日期/時段的派送結果統計與明細。
        # 為什麼：營運需要快速判斷 sent/skipped/failed 並追蹤錯誤原因。
        normalized_window = self._normalize_window(window=window, allow_empty=True)
        normalized_status = str(status or '').strip().lower()
        safe_limit = max(10, min(int(limit or 100), 500))

        query = (
            db.query(MonitoringReminderJob, RenalPatient)
            .join(RenalPatient, RenalPatient.id == MonitoringReminderJob.patient_id)
            .filter(MonitoringReminderJob.target_date == target_date)
        )
        if normalized_window:
            query = query.filter(MonitoringReminderJob.window == normalized_window)
        if normalized_status:
            query = query.filter(MonitoringReminderJob.status == normalized_status)

        rows = query.order_by(MonitoringReminderJob.created_at.desc()).limit(safe_limit).all()
        summary_query = db.query(MonitoringReminderJob.status, MonitoringReminderJob.id).filter(MonitoringReminderJob.target_date == target_date)
        if normalized_window:
            summary_query = summary_query.filter(MonitoringReminderJob.window == normalized_window)
        summary_rows = summary_query.all()
        summary_counts = {'pending': 0, 'sent': 0, 'skipped': 0, 'failed': 0}
        for status_value, _ in summary_rows:
            key = str(status_value or '').strip().lower()
            if key in summary_counts:
                summary_counts[key] += 1

        return {
            'ok': True,
            'date': target_date.isoformat(),
            'window': normalized_window or 'ALL',
            'status_filter': normalized_status or 'all',
            'summary': {
                'total_jobs': len(summary_rows),
                'pending': summary_counts['pending'],
                'sent': summary_counts['sent'],
                'skipped': summary_counts['skipped'],
                'failed': summary_counts['failed'],
            },
            'jobs': [
                {
                    'job_id': str(job.id),
                    'patient_id': str(patient.patient_code or ''),
                    'patient_name': str(patient.display_name or ''),
                    'window': str(job.window or ''),
                    'status': str(job.status or ''),
                    'scheduled_at': job.scheduled_at.isoformat() if job.scheduled_at else None,
                    'processed_at': job.processed_at.isoformat() if job.processed_at else None,
                    'error_message': str(job.error_message or ''),
                    'payload': job.payload if isinstance(job.payload, dict) else {},
                }
                for job, patient in rows
            ],
        }

    async def dispatch_reminders(self, *, db: Session, target_date: date, window: str, force: bool = False) -> dict:
        # 目的：依指定時段派送缺報提醒。
        # 為什麼：排程任務需要批次觸發並留下 job 與 delivery log 追溯。
        normalized_window = self._normalize_window(window=window)

        policy = self._get_or_create_default_policy(db=db, window=normalized_window)
        patient_rows = db.query(RenalPatient).filter(RenalPatient.enabled == True).all()  # noqa: E712
        active_binding_map = self._build_active_line_binding_map(db=db)
        scheduled_count = 0
        sent_count = 0
        skipped_count = 0
        failed_count = 0

        for patient in patient_rows:
            compliance_item = self._build_patient_compliance_item(
                db=db,
                patient=patient,
                target_date=target_date,
                active_binding_map=active_binding_map,
            )
            if not self._needs_reminder(compliance_item=compliance_item, window=normalized_window):
                continue
            if (not force) and self._has_existing_dispatched_job(
                db=db,
                patient_id=patient.id,
                target_date=target_date,
                window=normalized_window,
            ):
                continue
            scheduled_count += 1

            reminder_text = self._render_reminder_text(
                patient_name=str(patient.display_name or '').strip() or '腎友',
                window=normalized_window,
                is_diabetic=bool(getattr(patient, 'is_diabetic', False)),
                message_template=str(policy.message_template or '').strip(),
            )
            line_user_id = self._resolve_patient_line_user_id(patient=patient, active_binding_map=active_binding_map)
            job = MonitoringReminderJob(
                policy_id=policy.id,
                patient_id=patient.id,
                target_date=target_date,
                window=normalized_window,
                status='pending',
                scheduled_at=datetime.combine(target_date, time(0, 0)),
                payload={
                    'patient_code': str(patient.patient_code or ''),
                    'window': normalized_window,
                    'message': reminder_text,
                },
            )
            db.add(job)
            db.flush()

            if not line_user_id:
                job.status = 'skipped'
                job.processed_at = datetime.now()
                job.error_message = '病患尚未綁定 line_user_id'
                self._append_delivery_log(
                    db=db,
                    job_id=job.id,
                    patient_id=patient.id,
                    line_user_id=None,
                    delivery_status='skipped',
                    detail='病患尚未綁定 line_user_id',
                )
                skipped_count += 1
                continue

            try:
                await self._push_line_message(to_line_user_id=line_user_id, text=reminder_text)
                try:
                    self._append_line_console_message(
                        db=db,
                        line_user_id=line_user_id,
                        text=reminder_text,
                    )
                except Exception as sync_error:
                    logger.warning('monitoring_reminder_sync_line_console_failed', error=str(sync_error)[:300])
                job.status = 'sent'
                job.processed_at = datetime.now()
                self._append_delivery_log(
                    db=db,
                    job_id=job.id,
                    patient_id=patient.id,
                    line_user_id=line_user_id,
                    delivery_status='sent',
                    detail='提醒已送出',
                )
                sent_count += 1
            except Exception as error:
                job.status = 'failed'
                job.processed_at = datetime.now()
                job.error_message = str(error)[:300]
                self._append_delivery_log(
                    db=db,
                    job_id=job.id,
                    patient_id=patient.id,
                    line_user_id=line_user_id,
                    delivery_status='failed',
                    detail=str(error)[:300],
                )
                failed_count += 1

        db.commit()
        return {
            'ok': True,
            'date': target_date.isoformat(),
            'window': normalized_window,
            'scheduled': scheduled_count,
            'sent': sent_count,
            'skipped': skipped_count,
            'failed': failed_count,
        }

    def _normalize_window(self, *, window: str, allow_empty: bool = False) -> str:
        normalized_window = str(window or '').strip().upper()
        if allow_empty and not normalized_window:
            return ''
        if normalized_window not in {'MORNING', 'EVENING'}:
            raise validation_error('window 僅支援 MORNING 或 EVENING')
        return normalized_window

    def _serialize_policy(self, *, policy: MonitoringReminderPolicy) -> dict:
        return {
            'id': str(policy.id),
            'window': str(policy.window or ''),
            'channel': str(policy.channel or 'line'),
            'enabled': bool(policy.enabled),
            'requires_glucose_for_diabetic': bool(policy.requires_glucose_for_diabetic),
            'message_template': str(policy.message_template or ''),
            'updated_at': policy.updated_at.isoformat() if policy.updated_at else None,
        }

    def _has_existing_dispatched_job(self, *, db: Session, patient_id, target_date: date, window: str) -> bool:
        existing = (
            db.query(MonitoringReminderJob)
            .filter(
                MonitoringReminderJob.patient_id == patient_id,
                MonitoringReminderJob.target_date == target_date,
                MonitoringReminderJob.window == str(window or '').strip().upper(),
                MonitoringReminderJob.status.in_(['pending', 'sent', 'skipped']),
            )
            .first()
        )
        return existing is not None

    def _build_patient_compliance_item(
        self,
        *,
        db: Session,
        patient: RenalPatient,
        target_date: date,
        active_binding_map: dict[str, str],
    ) -> dict:
        day_start = datetime.combine(target_date, time.min)
        day_end = datetime.combine(target_date, time.max)
        rows = (
            db.query(MonitoringRecord)
            .filter(
                MonitoringRecord.patient_id == patient.id,
                MonitoringRecord.recorded_at >= day_start,
                MonitoringRecord.recorded_at <= day_end,
            )
            .all()
        )

        morning_rows = [row for row in rows if str(row.record_type or '') == 'MORNING']
        evening_rows = [row for row in rows if str(row.record_type or '') == 'EVENING']
        glucose_complete = True
        if bool(getattr(patient, 'is_diabetic', False)):
            glucose_complete = self._has_glucose_for_window(rows=morning_rows) and self._has_glucose_for_window(rows=evening_rows)
        resolved_line_user_id = self._resolve_patient_line_user_id(patient=patient, active_binding_map=active_binding_map)

        return {
            'patient_id': str(patient.patient_code or ''),
            'patient_name': str(patient.display_name or ''),
            'is_diabetic': bool(getattr(patient, 'is_diabetic', False)),
            'line_user_bound': bool(resolved_line_user_id),
            'morning_done': len(morning_rows) > 0,
            'evening_done': len(evening_rows) > 0,
            'glucose_complete': glucose_complete,
            'compliant': len(morning_rows) > 0 and len(evening_rows) > 0 and glucose_complete,
        }

    def _resolve_patient_line_user_id(self, *, patient: RenalPatient, active_binding_map: dict[str, str]) -> str:
        active_session_line_user_id = str(active_binding_map.get(str(patient.id), '') or '').strip()
        if active_session_line_user_id:
            return active_session_line_user_id
        patient_line_user_id = str(getattr(patient, 'line_user_id', '') or '').strip()
        patient_phone_number = str(getattr(patient, 'tel_no', '') or '').strip()
        if patient_line_user_id and patient_phone_number:
            return patient_line_user_id
        return ''

    def _build_active_line_binding_map(self, *, db: Session) -> dict[str, str]:
        # 目的：建立病患與 LINE user 的有效綁定映射。
        # 為什麼：病患主檔 line_user_id 可能尚未回填，但 session 已完成綁定，需避免誤判成未綁定。
        mapping: dict[str, str] = {}
        session_rows = (
            db.query(LineChannelSession)
            .filter(
                LineChannelSession.bound_patient_id.isnot(None),
                LineChannelSession.status == 'active',
                LineChannelSession.binding_status == 'bound',
            )
            .order_by(LineChannelSession.updated_at.desc())
            .all()
        )
        for session_row in session_rows:
            patient_id = str(getattr(session_row, 'bound_patient_id', '') or '').strip()
            if not patient_id or patient_id in mapping:
                continue
            line_user_id = str(getattr(session_row, 'line_user_id', '') or '').strip()
            if not line_user_id:
                continue
            mapping[patient_id] = line_user_id
        return mapping

    def _has_glucose_for_window(self, *, rows: list[MonitoringRecord]) -> bool:
        for row in rows:
            measurements = row.measurements if isinstance(row.measurements, dict) else {}
            if measurements.get('blood_glucose_mg_dl') is not None:
                return True
        return False

    def _needs_reminder(self, *, compliance_item: dict, window: str) -> bool:
        normalized_window = str(window or '').strip().upper()
        if normalized_window == 'MORNING':
            return not bool(compliance_item.get('morning_done'))
        if normalized_window == 'EVENING':
            if not bool(compliance_item.get('evening_done')):
                return True
            if bool(compliance_item.get('is_diabetic')) and not bool(compliance_item.get('glucose_complete')):
                return True
        return False

    def _get_or_create_default_policy(self, *, db: Session, window: str) -> MonitoringReminderPolicy:
        # 目的：提供提醒派送預設 policy，並相容升級舊版模板內容。
        # 為什麼：舊模板可能寫死 MORNING/EVENING 且缺少格式提示，需在讀取時自動修正。
        normalized_window = str(window or '').strip().upper()
        policy = (
            db.query(MonitoringReminderPolicy)
            .filter(
                MonitoringReminderPolicy.patient_id.is_(None),
                MonitoringReminderPolicy.window == normalized_window,
                MonitoringReminderPolicy.enabled == True,  # noqa: E712
            )
            .first()
        )
        if policy is not None:
            upgraded_template = self._build_default_reminder_template(window=normalized_window)
            if self._should_upgrade_template(message_template=str(policy.message_template or '').strip(), window=normalized_window):
                policy.message_template = upgraded_template
                db.add(policy)
                db.flush()
            return policy

        policy = MonitoringReminderPolicy(
            patient_id=None,
            channel='line',
            window=normalized_window,
            requires_glucose_for_diabetic=True,
            message_template=self._build_default_reminder_template(window=normalized_window),
            enabled=True,
        )
        db.add(policy)
        db.flush()
        return policy

    def _build_default_reminder_template(self, *, window: str) -> str:
        normalized_window = '早晨' if str(window or '').strip().upper() == 'MORNING' else '晚間'
        return (
            '{patient_name} 您好，請完成{window}回報：血壓、體重{glucose_hint}。'
            '格式範例：血壓 128/76、體重 63.4kg{glucose_example}'
        ).replace('{window}', normalized_window)

    def _should_upgrade_template(self, *, message_template: str, window: str) -> bool:
        normalized_text = str(message_template or '').strip()
        if not normalized_text:
            return True
        legacy_window = str(window or '').strip().upper()
        if f'完成{legacy_window}回報' in normalized_text:
            return True
        if '格式範例' not in normalized_text:
            return True
        return False

    def _render_reminder_text(self, *, patient_name: str, window: str, is_diabetic: bool, message_template: str) -> str:
        normalized_patient_name = str(patient_name or '').strip() or '腎友'
        normalized_window = '早晨' if str(window or '').strip().upper() == 'MORNING' else '晚間'
        glucose_hint = '，並補上血糖' if is_diabetic else ''
        glucose_example = '、血糖 142 mg/dL' if is_diabetic else ''
        template = str(message_template or '').strip() or self._build_default_reminder_template(window=str(window or '').strip().upper())
        return (
            template.replace('{patient_name}', normalized_patient_name)
            .replace('{window}', normalized_window)
            .replace('{glucose_hint}', glucose_hint)
            .replace('{glucose_example}', glucose_example)
        )

    def _append_line_console_message(self, *, db: Session, line_user_id: str, text: str) -> None:
        # 目的：將催報訊息同步到 LINE 對話中心訊息紀錄。
        # 為什麼：若只 push 不寫 line_messages，護理師後台會看不到系統實際發送內容。
        session = line_onboarding_service.get_or_create_line_session(db=db, line_user_id=line_user_id)
        session = line_onboarding_service.ensure_renal_companion_assignment(db=db, session=session)
        db.add(
            LineMessage(
                session_id=session.id,
                conversation_id=session.conversation_id,
                direction='outbound',
                sender_type='system',
                content=str(text or '').strip(),
            )
        )
        db.add(
            Message(
                conversation_id=session.conversation_id,
                role='assistant',
                content=f"[系統提醒] {str(text or '').strip()}",
                timestamp=datetime.now(),
            )
        )
        session.last_outbound_at = datetime.now()
        db.add(session)

    def _append_delivery_log(
        self,
        *,
        db: Session,
        job_id,
        patient_id,
        line_user_id: str | None,
        delivery_status: str,
        detail: str,
    ) -> None:
        db.add(
            MonitoringReminderDeliveryLog(
                job_id=job_id,
                patient_id=patient_id,
                line_user_id=str(line_user_id or '').strip() or None,
                delivery_status=delivery_status,
                detail=str(detail or '').strip() or None,
            )
        )

    async def _push_line_message(self, *, to_line_user_id: str, text: str) -> None:
        channel_access_token = str(getattr(settings, 'LINE_CHANNEL_ACCESS_TOKEN', '') or '').strip()
        if not channel_access_token:
            raise validation_error('LINE_CHANNEL_ACCESS_TOKEN 尚未設定')
        payload = {
            'to': str(to_line_user_id or '').strip(),
            'messages': [
                {
                    'type': 'text',
                    'text': str(text or '').strip()[:5000],
                }
            ],
        }
        headers = {
            'Authorization': f'Bearer {channel_access_token}',
            'Content-Type': 'application/json',
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post('https://api.line.me/v2/bot/message/push', headers=headers, json=payload)
        if response.status_code >= 400:
            raise validation_error(f'LINE push 失敗: {response.status_code} {response.text[:200]}')


monitoring_reminder_service = MonitoringReminderService()
