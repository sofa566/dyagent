from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from src.api.errors import validation_error
from src.core.config import settings
from src.models import Agent, Conversation, LineChannelSession, RenalPatient
from src.services.redis_service import redis_service


class LineOnboardingService:
    # 目的：集中處理 LINE 新好友與綁定流程（follow/message/unfollow）狀態機。
    # 為什麼：將 webhook 事件判斷與綁定規則從 route 分離，降低路由層複雜度並便於測試。

    EVENT_DEDUP_SECONDS = 24 * 60 * 60

    def resolve_default_agent(self, db: Session) -> Agent:
        return self._pick_default_agent(db)

    def resolve_renal_companion_agent(self, db: Session) -> Agent:
        # 目的：解析 LINE 腎友專屬代理者。
        # 為什麼：腎友訊息須固定走同一照護代理，避免回退主代理造成流程不一致。
        configured_agent_id = str(getattr(settings, 'LINE_RENAL_COMPANION_AGENT_ID', '') or '').strip()
        if configured_agent_id:
            row = db.query(Agent).filter(Agent.id == configured_agent_id, Agent.enabled == True).first()  # noqa: E712
            if row is not None:
                return row

        configured_name = str(getattr(settings, 'LINE_RENAL_COMPANION_AGENT_NAME', '') or '').strip()
        if configured_name:
            row = db.query(Agent).filter(Agent.name == configured_name, Agent.enabled == True).first()  # noqa: E712
            if row is not None:
                return row

        return self._pick_default_agent(db)

    def _pick_default_agent(self, db: Session) -> Agent:
        # 目的：選擇 LINE 會話的預設代理者。
        # 為什麼：建立新會話時必須有可用 agent，避免後續無法進行對話與回覆。
        configured_agent_id = str(getattr(settings, 'LINE_DEFAULT_AGENT_ID', '') or '').strip()
        if configured_agent_id:
            row = db.query(Agent).filter(Agent.id == configured_agent_id, Agent.enabled == True).first()  # noqa: E712
            if row is not None:
                return row
        router_agent = db.query(Agent).filter(Agent.is_router == True, Agent.enabled == True).first()  # noqa: E712
        if router_agent is not None:
            return router_agent
        fallback_agent = db.query(Agent).filter(Agent.enabled == True).first()  # noqa: E712
        if fallback_agent is None:
            raise validation_error('尚無可用 Agent，無法處理 LINE 訊息')
        return fallback_agent

    def _normalize_phone(self, raw_value: str) -> str:
        normalized = ''.join([char for char in str(raw_value or '') if char.isdigit()])
        if normalized.startswith('886'):
            local_candidate = normalized[3:]
            if local_candidate and not local_candidate.startswith('0'):
                local_candidate = f'0{local_candidate}'
            return local_candidate
        return normalized

    def _build_auto_patient_code(self) -> str:
        return f'AUTO-{uuid.uuid4().hex[:8].upper()}'

    def _find_patient_by_line_user(self, *, db: Session, line_user_id: str) -> RenalPatient | None:
        normalized_line_user_id = str(line_user_id or '').strip()
        if not normalized_line_user_id:
            return None
        return (
            db.query(RenalPatient)
            .filter(RenalPatient.enabled == True, RenalPatient.line_user_id == normalized_line_user_id)  # noqa: E712
            .first()
        )

    def _list_patients_by_binding_identity(self, *, db: Session, name: str, phone: str) -> list[RenalPatient]:
        # 目的：以姓名 + 手機比對候選腎友，並處理手機格式差異。
        # 為什麼：既有資料可能有 09 / +886 / 連字號等不同格式，需統一比對避免漏綁。
        normalized_name = str(name or '').strip()
        normalized_phone = self._normalize_phone(phone)
        if not normalized_name or not normalized_phone:
            return []

        name_rows = (
            db.query(RenalPatient)
            .filter(
                RenalPatient.enabled == True,  # noqa: E712
                RenalPatient.display_name == normalized_name,
            )
            .all()
        )
        return [row for row in name_rows if self._normalize_phone(str(row.tel_no or '')) == normalized_phone]

    def _sync_session_binding_from_patient(self, *, session: LineChannelSession, patient: RenalPatient) -> None:
        session.bound_patient_id = patient.id
        session.binding_status = 'bound'
        session.binding_name = str(patient.display_name or '').strip() or None
        session.binding_phone = str(patient.tel_no or '').strip() or None
        if session.bound_at is None:
            session.bound_at = datetime.now()

    def _save_session(self, *, db: Session, session: LineChannelSession) -> None:
        db.add(session)
        db.commit()
        db.refresh(session)

    def get_or_create_line_session(self, *, db: Session, line_user_id: str) -> LineChannelSession:
        # 目的：查找或建立 LINE 對話 Session。
        # 為什麼：不同事件（follow/message/unfollow）都需要共用相同 session 身分與狀態。
        normalized_line_user_id = str(line_user_id or '').strip()
        if not normalized_line_user_id:
            raise validation_error('line_user_id 不可為空')

        session = db.query(LineChannelSession).filter(LineChannelSession.line_user_id == normalized_line_user_id).first()
        if session is not None:
            return self.ensure_renal_companion_assignment(db=db, session=session)

        target_agent = self.resolve_renal_companion_agent(db)
        conversation = Conversation(
            user_id=None,
            agent_id=target_agent.id,
            title=f'LINE-{normalized_line_user_id[:12]}',
        )
        db.add(conversation)
        db.flush()

        session = LineChannelSession(
            line_user_id=normalized_line_user_id,
            conversation_id=conversation.id,
            assigned_agent_id=target_agent.id,
            bound_patient_id=None,
            binding_status='pending_name',
            binding_name=None,
            binding_phone=None,
            bound_at=None,
            mode='bot',
            status='active',
            last_inbound_at=datetime.now(),
            last_outbound_at=None,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    def ensure_renal_companion_assignment(self, *, db: Session, session: LineChannelSession) -> LineChannelSession:
        # 目的：確保 LINE session 一律由腎友陪伴代理處理。
        # 為什麼：避免既有 session 綁到其他代理而讓腎友流程混入一般聊天邏輯。
        target_agent = self.resolve_renal_companion_agent(db)
        if str(session.assigned_agent_id or '') == str(target_agent.id):
            return session
        session.assigned_agent_id = target_agent.id
        db.add(session)
        db.query(Conversation).filter(Conversation.id == session.conversation_id).update({'agent_id': target_agent.id})
        db.commit()
        db.refresh(session)
        return session

    def build_bound_patient_summary(self, *, db: Session, session: LineChannelSession) -> dict:
        # 目的：提供 session 對應病患摘要供 API 回傳。
        # 為什麼：前端列表需要一致的綁定狀態欄位，不應重複查詢邏輯於 route。
        patient = None
        if session.bound_patient_id:
            patient = db.query(RenalPatient).filter(RenalPatient.id == session.bound_patient_id).first()
        if patient is None:
            patient = self._find_patient_by_line_user(db=db, line_user_id=str(session.line_user_id or '').strip())
        if patient is None:
            return {'bound': False, 'patient_id': '', 'name': ''}
        return {
            'bound': True,
            'patient_id': str(patient.patient_code or ''),
            'name': str(patient.display_name or ''),
        }

    async def should_skip_event(self, *, event: dict) -> bool:
        # 目的：針對 webhook 事件做去重，避免 LINE 重送造成重複處理。
        # 為什麼：重複事件會導致重複推播、重複寫入訊息與錯誤的狀態遷移。
        if redis_service.client is None:
            return False
        dedup_key = self._build_event_dedup_key(event)
        if not dedup_key:
            return False
        created = await redis_service.client.set(
            dedup_key,
            '1',
            ex=self.EVENT_DEDUP_SECONDS,
            nx=True,
        )
        return not bool(created)

    async def should_send_follow_welcome(self, *, line_user_id: str) -> bool:
        # 目的：限制 welcome 訊息在 24 小時內只發送一次。
        # 為什麼：同一使用者重複 follow 或 webhook 重放時，不應造成重複歡迎訊息干擾。
        normalized_line_user_id = str(line_user_id or '').strip()
        if not normalized_line_user_id:
            return False
        if redis_service.client is None:
            return True
        welcome_key = f'line:webhook:welcome:{normalized_line_user_id}'
        created = await redis_service.client.set(
            welcome_key,
            '1',
            ex=self.EVENT_DEDUP_SECONDS,
            nx=True,
        )
        return bool(created)

    def _build_event_dedup_key(self, event: dict) -> str:
        # 目的：產生 webhook 事件去重 key。
        # 為什麼：優先使用官方 webhookEventId，缺漏時以事件指紋退化，確保可去重。
        event_type = str((event or {}).get('type') or '').strip()
        if not event_type:
            return ''
        event_id = str((event or {}).get('webhookEventId') or '').strip()
        source = (event or {}).get('source') if isinstance((event or {}).get('source'), dict) else {}
        line_user_id = str(source.get('userId') or '').strip()
        timestamp = str((event or {}).get('timestamp') or '').strip()
        if event_id:
            return f'line:webhook:event:{event_id}'
        message = (event or {}).get('message') if isinstance((event or {}).get('message'), dict) else {}
        message_id = str(message.get('id') or '').strip()
        fallback = '|'.join([event_type, line_user_id, message_id, timestamp]).strip('|')
        if not fallback:
            return ''
        return f'line:webhook:event:fallback:{fallback}'

    def handle_follow_event(self, *, db: Session, line_user_id: str) -> tuple[LineChannelSession, str | None]:
        # 目的：處理 LINE 新好友事件並啟動綁定導引。
        # 為什麼：新好友通常尚未主動發訊，需由 follow 事件主動開始 onboarding。
        session = self.get_or_create_line_session(db=db, line_user_id=line_user_id)
        session.status = 'active'

        existing_patient = self._find_patient_by_line_user(db=db, line_user_id=session.line_user_id)
        if existing_patient is not None:
            self._sync_session_binding_from_patient(session=session, patient=existing_patient)
            self._save_session(db=db, session=session)
            return session, None

        current_status = str(session.binding_status or 'pending_name').strip().lower()
        if current_status in {'pending_name', 'awaiting_name'}:
            session.binding_status = 'awaiting_name'
            self._save_session(db=db, session=session)
            return session, '您好，感謝加入 LINE 腎友照護。為了完成資料綁定，請先回覆您真實的姓名。'

        self._save_session(db=db, session=session)
        return session, None

    def handle_unfollow_event(self, *, db: Session, line_user_id: str) -> None:
        normalized_line_user_id = str(line_user_id or '').strip()
        if not normalized_line_user_id:
            return
        session = db.query(LineChannelSession).filter(LineChannelSession.line_user_id == normalized_line_user_id).first()
        if session is None:
            return
        session.status = 'inactive'
        db.add(session)
        db.commit()

    def try_auto_bind_patient_from_dialog(self, *, db: Session, session: LineChannelSession, inbound_text: str) -> tuple[bool, str | None]:
        # 目的：在 LINE 文字對話中執行姓名+手機綁定狀態機。
        # 為什麼：病患首輪對話需先確立身份，後續監測與衛教才能落在正確主檔。
        existing_patient = self._find_patient_by_line_user(db=db, line_user_id=session.line_user_id)
        if existing_patient is not None:
            self._sync_session_binding_from_patient(session=session, patient=existing_patient)
            self._save_session(db=db, session=session)
            return False, None

        binding_status = str(session.binding_status or 'pending_name').strip().lower()
        normalized_text = str(inbound_text or '').strip()
        if binding_status in {'pending_name', 'awaiting_name', 'manual_review'}:
            if binding_status == 'pending_name':
                session.binding_status = 'awaiting_name'
                self._save_session(db=db, session=session)
                return True, '您好，為了完成腎友資料綁定，請先回覆您的姓名。'
            if binding_status == 'manual_review':
                return True, '目前此 LINE 帳號需要人工協助綁定，請稍候主護理師聯繫。'
            if len(normalized_text) < 2:
                return True, '姓名長度不足，請重新輸入真實姓名。'
            if self._normalize_phone(normalized_text):
                return True, '請先回覆姓名，再回覆手機號碼。'
            session.binding_name = normalized_text
            session.binding_status = 'awaiting_phone'
            self._save_session(db=db, session=session)
            return True, f'收到，{normalized_text} 您好。請回覆手機號碼（只需數字）。'

        if binding_status == 'awaiting_phone':
            normalized_phone = self._normalize_phone(normalized_text)
            if len(normalized_phone) < 8 or len(normalized_phone) > 12:
                return True, '手機格式不正確，請輸入 8 到 12 位數字。'

            patient_name = str(session.binding_name or '').strip()
            if not patient_name:
                session.binding_status = 'awaiting_name'
                self._save_session(db=db, session=session)
                return True, '尚未取得姓名，請先回覆您的姓名。'

            matched_rows = self._list_patients_by_binding_identity(db=db, name=patient_name, phone=normalized_phone)
            if len(matched_rows) > 1:
                session.binding_status = 'manual_review'
                self._save_session(db=db, session=session)
                return True, '找到多筆同姓名與手機資料，已轉人工協助綁定。'

            matched_patient = matched_rows[0] if matched_rows else None
            if matched_patient is None:
                matched_patient = RenalPatient(
                    patient_code=self._build_auto_patient_code(),
                    display_name=patient_name,
                    tel_no=normalized_phone,
                    line_user_id=str(session.line_user_id or '').strip(),
                    med_history='LINE 首次對話自動建立基本檔',
                    enabled=True,
                )
                db.add(matched_patient)
                db.flush()
                self._sync_session_binding_from_patient(session=session, patient=matched_patient)
                session.binding_phone = normalized_phone
                self._save_session(db=db, session=session)
                return True, f'已建立您的腎友基本資料（{patient_name}）並完成綁定，請每日早晚回報你的血壓與體重，平時若有不舒服狀況，可隨時回報，也歡迎你提出其他需求。'

            current_line_user_id = str(matched_patient.line_user_id or '').strip()
            session_line_user_id = str(session.line_user_id or '').strip()
            if current_line_user_id and current_line_user_id != session_line_user_id:
                session.binding_status = 'manual_review'
                self._save_session(db=db, session=session)
                return True, '找到同姓名與手機的腎友資料，但已綁定其他 LINE 帳號，已轉人工協助。'

            matched_patient.line_user_id = session_line_user_id
            db.add(matched_patient)
            db.flush()
            self._sync_session_binding_from_patient(session=session, patient=matched_patient)
            session.binding_phone = normalized_phone
            self._save_session(db=db, session=session)
            return True, f'已找到您的腎友基本資料並完成綁定，{patient_name} 您可以開始回報今日身體狀況。'

        session.binding_status = 'awaiting_name'
        self._save_session(db=db, session=session)
        return True, '請先回覆姓名以完成綁定。'


line_onboarding_service = LineOnboardingService()
