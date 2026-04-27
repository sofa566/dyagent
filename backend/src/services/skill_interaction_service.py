from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
import uuid

from sqlalchemy.orm import Session

from src.core.config import settings
from src.models import SkillInteraction
from src.services.skill_ui_token_service import create_skill_ui_token


@dataclass
class InteractionPrepareResult:
    ok: bool
    error: str | None
    action: str
    interaction: SkillInteraction | None
    skill_payload: dict[str, Any]


class SkillInteractionService:
    """目的：管理技能互動狀態的建立、讀取與狀態轉移。
    為什麼：將流程控制從 ChatRouter 抽離，避免工具執行邏輯與互動生命週期耦合。
    """

    def __init__(self, db: Session):
        self._db = db

    def prepare_request(self, *, conversation_id: str, tool_name: str, skill_id: str | None, payload: dict[str, Any]) -> InteractionPrepareResult:
        # 目的：在技能執行前解析 action 與 interaction，回傳可供技能使用的 payload。
        # 為什麼：前端會送 envelope（action/form_data），需先轉成技能可驗證的輸入。
        source_payload = payload if isinstance(payload, dict) else {}
        action = self._normalize_action(source_payload)
        interaction_id_text = str(source_payload.get('interaction_id') or '').strip()

        if not action and not interaction_id_text:
            return InteractionPrepareResult(ok=True, error=None, action='direct', interaction=None, skill_payload=source_payload)

        if action == 'start' and not interaction_id_text:
            interaction = self._create_interaction_with_retry(conversation_id=conversation_id, tool_name=tool_name, skill_id=skill_id)
            return InteractionPrepareResult(
                ok=True,
                error=None,
                action='start',
                interaction=interaction,
                skill_payload=self._build_skill_payload(source_payload=source_payload, action='start', interaction=interaction),
            )

        if not interaction_id_text:
            return InteractionPrepareResult(ok=False, error='missing_interaction_id', action=action or 'submit', interaction=None, skill_payload={})

        interaction = self._find_interaction(interaction_id=interaction_id_text, conversation_id=conversation_id, tool_name=tool_name)
        if interaction is None:
            return InteractionPrepareResult(ok=False, error='interaction_not_found', action=action or 'submit', interaction=None, skill_payload={})

        if self._is_expired(interaction):
            interaction.status = 'expired'
            self._db.commit()
            return InteractionPrepareResult(ok=False, error='interaction_expired', action=action or 'submit', interaction=interaction, skill_payload={})

        if str(interaction.status or 'active') != 'active' and action not in {'resume', 'heartbeat'}:
            return InteractionPrepareResult(ok=False, error='interaction_inactive', action=action or 'submit', interaction=interaction, skill_payload={})

        if action == 'heartbeat':
            self._extend_expiration(interaction)
            return InteractionPrepareResult(
                ok=True,
                error=None,
                action='heartbeat',
                interaction=interaction,
                skill_payload=self._build_skill_payload(source_payload=source_payload, action='heartbeat', interaction=interaction),
            )

        if action == 'cancel':
            interaction.status = 'cancelled'
            self._db.commit()

        return InteractionPrepareResult(
            ok=True,
            error=None,
            action=action or 'submit',
            interaction=interaction,
            skill_payload=self._build_skill_payload(source_payload=source_payload, action=action or 'submit', interaction=interaction),
        )

    def finalize_success(self, *, prepare_result: InteractionPrepareResult, result_payload: dict[str, Any]) -> dict[str, Any]:
        # 目的：根據技能 mode 更新互動狀態，並補齊 interaction_id 回傳。
        # 為什麼：讓前端每輪都能拿到可追蹤 id，並維持資料庫狀態一致。
        if not isinstance(result_payload, dict):
            return result_payload

        interaction = prepare_result.interaction
        result_obj = result_payload.get('result') if isinstance(result_payload.get('result'), dict) else None
        if interaction is None or result_obj is None:
            return result_payload

        mode_text = str(result_obj.get('mode') or '').strip().lower()
        result_obj['interaction_id'] = str(interaction.id)

        if mode_text == 'ui':
            interaction.status = 'active'
            if result_obj.get('step') is not None:
                interaction.current_step = str(result_obj.get('step'))
            ui_obj = result_obj.get('ui') if isinstance(result_obj.get('ui'), dict) else {}
            if isinstance(ui_obj.get('state'), dict):
                incoming_state = ui_obj.get('state') or {}
                is_heartbeat_action = str(prepare_result.action or '') == 'heartbeat'
                if incoming_state or (not is_heartbeat_action):
                    interaction.state_json = incoming_state
            entry_path = str(ui_obj.get('entry') or '').strip().lstrip('/')
            if entry_path:
                ui_token = create_skill_ui_token(
                    conversation_id=str(interaction.conversation_id),
                    tool_name=str(interaction.tool_name),
                    interaction_id=str(interaction.id),
                    expires_in_seconds=int(settings.SKILL_UI_TOKEN_TTL_SEC or 600),
                )
                ui_obj['ui_url'] = f"/api/skills/ui/{interaction.id}/{entry_path}?token={ui_token}"
            result_obj['ui'] = ui_obj
            result_obj['channel_nonce'] = str(interaction.ui_session_nonce or '')
            self._extend_expiration(interaction)
            return result_payload

        if mode_text == 'final':
            interaction.status = 'completed'
            interaction.current_step = str(result_obj.get('step') or interaction.current_step or '') or None
            self._db.commit()
            return result_payload

        if mode_text == 'error':
            interaction.status = 'failed'
            self._db.commit()
            return result_payload

        self._extend_expiration(interaction)
        return result_payload

    def finalize_error(self, *, prepare_result: InteractionPrepareResult, error_text: str) -> None:
        interaction = prepare_result.interaction
        if interaction is None:
            return
        if str(interaction.status or 'active') != 'cancelled':
            interaction.status = 'failed'
            interaction.current_step = str(prepare_result.skill_payload.get('_interaction', {}).get('step') or interaction.current_step or '') or None
        self._db.commit()

    def _normalize_action(self, payload: dict[str, Any]) -> str:
        raw_action = str(payload.get('action') or '').strip().lower()
        if raw_action in {'start', 'submit', 'back', 'cancel', 'resume', 'heartbeat'}:
            return raw_action
        return ''

    def _build_skill_payload(self, *, source_payload: dict[str, Any], action: str, interaction: SkillInteraction) -> dict[str, Any]:
        # 目的：將通用 envelope 轉為技能腳本可直接處理的輸入。
        # 為什麼：技能 schema 多半只定義業務欄位，需抽出 form_data 並附上互動上下文。
        submitted_data = source_payload.get('form_data')
        skill_payload = submitted_data if isinstance(submitted_data, dict) else {}
        if action == 'start' and not skill_payload:
            fallback_payload = {k: v for k, v in source_payload.items() if k not in {'action', 'interaction_id', 'step', 'form_data', 'client_meta'}}
            if fallback_payload:
                skill_payload = fallback_payload
        step_value = source_payload.get('step')
        if step_value is None:
            step_value = interaction.current_step
        wrapper = {
            '_interaction': {
                'id': str(interaction.id),
                'action': action,
                'step': step_value,
                'status': str(interaction.status or 'active'),
                'state': interaction.state_json if isinstance(interaction.state_json, dict) else {},
                'expires_at': interaction.expires_at.isoformat() if isinstance(interaction.expires_at, datetime) else None,
            }
        }
        if isinstance(skill_payload, dict):
            merged_payload = dict(skill_payload)
            merged_payload.update(wrapper)
            return merged_payload
        return wrapper

    def _create_interaction(self, *, conversation_id: str, tool_name: str, skill_id: str | None) -> SkillInteraction:
        # 目的：建立新互動實體並寫入初始 TTL。
        # 為什麼：start 不應依賴前端先提供 interaction_id，需由後端統一產生與管控。
        expires_at = datetime.now() + timedelta(seconds=max(60, int(settings.SKILL_INTERACTION_TTL_SEC or 1800)))
        interaction = SkillInteraction(
            id=uuid.uuid4(),
            conversation_id=conversation_id,
            tool_name=tool_name,
            skill_id=skill_id,
            status='active',
            current_step=None,
            state_json={},
            ui_session_nonce=str(uuid.uuid4()),
            expires_at=expires_at,
        )
        self._db.add(interaction)
        self._db.commit()
        self._db.refresh(interaction)
        return interaction

    def _create_interaction_with_retry(self, *, conversation_id: str, tool_name: str, skill_id: str | None) -> SkillInteraction:
        # 目的：在資料表尚未建立時自動補建並重試一次。
        # 為什麼：開發期可能尚未執行 migration，需避免互動流程直接失效。
        try:
            return self._create_interaction(conversation_id=conversation_id, tool_name=tool_name, skill_id=skill_id)
        except Exception as create_error:
            if not self._is_missing_table_error(create_error):
                raise
            try:
                self._db.rollback()
            except Exception:
                pass
            bind = self._db.get_bind()
            SkillInteraction.__table__.create(bind=bind, checkfirst=True)
            return self._create_interaction(conversation_id=conversation_id, tool_name=tool_name, skill_id=skill_id)

    def _is_missing_table_error(self, error: Exception) -> bool:
        error_text = str(error or '').lower()
        return ('undefinedtable' in error_text) or ('relation "skill_interactions" does not exist' in error_text)

    def _find_interaction(self, *, interaction_id: str, conversation_id: str, tool_name: str) -> SkillInteraction | None:
        try:
            parsed_id = uuid.UUID(str(interaction_id))
        except Exception:
            return None
        return (
            self._db.query(SkillInteraction)
            .filter(SkillInteraction.id == parsed_id)
            .filter(SkillInteraction.conversation_id == conversation_id)
            .filter(SkillInteraction.tool_name == tool_name)
            .first()
        )

    def _is_expired(self, interaction: SkillInteraction) -> bool:
        expires_at = interaction.expires_at if isinstance(interaction.expires_at, datetime) else None
        if expires_at is None:
            return False
        return datetime.now() > expires_at

    def _extend_expiration(self, interaction: SkillInteraction) -> None:
        interaction.expires_at = datetime.now() + timedelta(seconds=max(60, int(settings.SKILL_INTERACTION_TTL_SEC or 1800)))
        self._db.commit()
