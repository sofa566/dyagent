from __future__ import annotations

import base64
import hashlib
import hmac
import re
import uuid
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Body, Depends, Header, Request
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error, not_found_error, validation_error
from src.core.config import settings
from src.core.database import get_db
from src.core.logging import get_logger
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import (
    Agent,
    Conversation,
    LineChannelSession,
    LineMessage,
    Message,
    MonitoringRecord,
    RenalPatient,
    User,
)
from src.services.chat_router import ChatRouter
from src.services.line_onboarding_service import line_onboarding_service
from src.services.redis_service import redis_service
from src.services.renal_monitoring_service import renal_monitoring_service

router = APIRouter()
logger = get_logger(__name__)

MONITORING_DRAFT_TTL_SECONDS = 24 * 60 * 60


def _verify_line_signature(*, raw_body: bytes, header_signature: str) -> bool:
    # 目的：驗證 LINE webhook 簽章。
    # 為什麼：避免偽造來源直接寫入系統對話與觸發外部回覆。
    channel_secret = str(getattr(settings, 'LINE_CHANNEL_SECRET', '') or '').strip()
    if not channel_secret:
        return False
    expected = hmac.new(
        channel_secret.encode('utf-8'),
        raw_body,
        hashlib.sha256,
    ).digest()
    expected_text = base64.b64encode(expected).decode('utf-8')
    return hmac.compare_digest(expected_text, str(header_signature or '').strip())


def _require_line_operator_read_permission(*, current_user: User, db: Session) -> None:
    # 目的：限制 LINE 對話後台的讀取權限。
    # 為什麼：LINE 對話可能含個資，需綁定到照護/LINE 專用權限才能檢視。
    can_read = bool(
        check_permission(current_user, 'line.center', db=db)
        or check_permission(current_user, 'nursing.line', db=db)
        or check_permission(current_user, 'read_logs', db=db)
        or check_permission(current_user, 'update_agent', db=db)
    )
    if not can_read:
        raise forbidden_error('無權限檢視 LINE 對話')


def _require_line_operator_manage_permission(*, current_user: User, db: Session) -> None:
    # 目的：限制 LINE 對話操作（人工回覆、切換模式）權限。
    # 為什麼：避免一般使用者誤發外部訊息給客戶。
    can_manage = bool(
        check_permission(current_user, 'line.center', db=db)
        or check_permission(current_user, 'nursing.line', db=db)
        or check_permission(current_user, 'update_agent', db=db)
        or check_permission(current_user, 'read_logs', db=db)
    )
    if not can_manage:
        raise forbidden_error('無權限操作 LINE 對話')


def _store_line_message(
    *,
    db: Session,
    session: LineChannelSession,
    direction: str,
    sender_type: str,
    content: str,
    line_message_id: str | None = None,
    reply_token: str | None = None,
    operator_user_id: str | None = None,
) -> LineMessage:
    # 目的：統一寫入 LINE 通道訊息稽核。
    # 為什麼：後續需支援人工接手、對帳、重送與法遵追蹤。
    normalized_content = str(content or '').strip()
    if not normalized_content:
        raise validation_error('訊息內容不可為空')

    row = LineMessage(
        session_id=session.id,
        conversation_id=session.conversation_id,
        direction=direction,
        sender_type=sender_type,
        content=normalized_content,
        line_message_id=str(line_message_id or '').strip() or None,
        reply_token=str(reply_token or '').strip() or None,
        operator_user_id=operator_user_id,
    )
    db.add(row)
    return row


def _sync_message_to_conversation(*, db: Session, session: LineChannelSession, role: str, content: str) -> Message:
    # 目的：將 LINE 訊息同步到既有 Conversation/Message。
    # 為什麼：讓既有聊天頁與審計查詢可共用同一套訊息來源。
    row = Message(
        conversation_id=session.conversation_id,
        role=role,
        content=str(content or '').strip(),
        timestamp=datetime.now(),
    )
    db.add(row)
    db.query(Conversation).filter(Conversation.id == session.conversation_id).update({'last_interacted_at': datetime.now()})
    return row


def _run_agent_reply(*, db: Session, session: LineChannelSession, user_message: str) -> str:
    # 目的：以指定會話與代理者執行單輪回覆。
    # 為什麼：重用既有 ChatRouter 能力，避免為 LINE 另外維護一套對話引擎。
    agent_id = str(session.assigned_agent_id or '').strip()
    if not agent_id:
        target_agent = line_onboarding_service.resolve_renal_companion_agent(db)
        agent_id = str(target_agent.id)
        session.assigned_agent_id = target_agent.id
        db.add(session)
        db.commit()
        db.refresh(session)

    router = ChatRouter()
    router.set_execution_context(
        user_id='',
        agent_id=agent_id,
        conversation_id=str(session.conversation_id),
    )
    return str(
        router.single_turn(
            session_id=str(session.conversation_id),
            agent_id=agent_id,
            user_message=str(user_message or '').strip(),
            db=db,
        )
        or ''
    ).strip()


def _parse_blood_pressure(text: str) -> tuple[int, int] | None:
    matched = re.search(r'(\d{2,3})\s*/\s*(\d{2,3})', str(text or ''))
    if matched is None:
        return None
    systolic = int(matched.group(1))
    diastolic = int(matched.group(2))
    return systolic, diastolic


def _parse_weight_kg(text: str) -> float | None:
    # 目的：從 LINE 自由格式文字抽取體重（kg）。
    # 為什麼：病患常用「114/74，70.9」等簡寫，需支援無單位與逗號分隔情境。
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


def _parse_blood_glucose(text: str) -> int | None:
    patterns = [
        r'血糖\s*[:：]?\s*(\d{2,3})',
        r'glucose\s*[:：]?\s*(\d{2,3})',
    ]
    normalized_text = str(text or '').lower()
    for pattern in patterns:
        matched = re.search(pattern, normalized_text)
        if matched is not None:
            return int(matched.group(1))
    return None


def _resolve_monitoring_record_type(now: datetime) -> str:
    hour = int(now.hour)
    if hour < 15:
        return 'MORNING'
    return 'EVENING'


def _build_monitoring_draft_key(*, session: LineChannelSession, record_date: str, record_type: str) -> str:
    # 目的：產生 LINE 監測暫存 key。
    # 為什麼：需要在分開傳送時，將同一天同時段資料合併判斷是否可寫入。
    return f'line:monitoring:draft:{session.id}:{record_date}:{record_type}'


async def _load_monitoring_draft(*, session: LineChannelSession, record_date: str, record_type: str) -> dict:
    # 目的：讀取監測暫存草稿資料。
    # 為什麼：分訊息回報時必須保留上一則已收集的欄位，避免要求一次輸入全部。
    draft_key = _build_monitoring_draft_key(session=session, record_date=record_date, record_type=record_type)
    payload = await redis_service.get_json(draft_key)
    if not isinstance(payload, dict):
        return {}
    measurements = payload.get('measurements') if isinstance(payload.get('measurements'), dict) else {}
    return {'measurements': measurements}


async def _save_monitoring_draft(*, session: LineChannelSession, record_date: str, record_type: str, measurements: dict) -> None:
    # 目的：保存監測暫存草稿資料。
    # 為什麼：讓病患可以先傳血壓再補體重，或反向補齊後再完成正式入庫。
    draft_key = _build_monitoring_draft_key(session=session, record_date=record_date, record_type=record_type)
    await redis_service.set_json(
        draft_key,
        {'measurements': dict(measurements or {})},
        expire=MONITORING_DRAFT_TTL_SECONDS,
    )


async def _clear_monitoring_draft(*, session: LineChannelSession, record_date: str, record_type: str) -> None:
    draft_key = _build_monitoring_draft_key(session=session, record_date=record_date, record_type=record_type)
    await redis_service.delete(draft_key)


def _build_measurements_from_text(text: str) -> dict:
    # 目的：將單則文字統一轉成監測欄位字典。
    # 為什麼：避免各流程重複撰寫解析邏輯，並確保欄位命名一致。
    measurements: dict[str, int | float] = {}
    blood_pressure = _parse_blood_pressure(text)
    if blood_pressure is not None:
        measurements['systolic'], measurements['diastolic'] = blood_pressure
    weight_kg = _parse_weight_kg(text)
    if weight_kg is not None:
        measurements['weight_kg'] = weight_kg
    glucose_value = _parse_blood_glucose(text)
    if glucose_value is not None:
        measurements['blood_glucose_mg_dl'] = glucose_value
    return measurements


def _merge_measurements(*, draft_measurements: dict, incoming_measurements: dict) -> dict:
    merged = dict(draft_measurements or {})
    for field_name in ('systolic', 'diastolic', 'weight_kg', 'blood_glucose_mg_dl'):
        if incoming_measurements.get(field_name) is not None:
            merged[field_name] = incoming_measurements[field_name]
    return merged


def _list_missing_measurements(*, patient: RenalPatient, measurements: dict) -> list[str]:
    missing_fields: list[str] = []
    if measurements.get('systolic') is None or measurements.get('diastolic') is None:
        missing_fields.append('血壓')
    if measurements.get('weight_kg') is None:
        missing_fields.append('體重')
    if bool(getattr(patient, 'is_diabetic', False)) and measurements.get('blood_glucose_mg_dl') is None:
        missing_fields.append('血糖')
    return missing_fields


def _format_missing_measurement_prompt(*, missing_fields: list[str]) -> str:
    if not missing_fields:
        return ''
    return f"已收到部分資料，還缺 {', '.join(missing_fields)}。請補上後我會立即完成紀錄。"


def _find_existing_patient_record(
    *,
    db: Session,
    patient: RenalPatient,
    recorded_at: datetime,
    record_type: str,
) -> MonitoringRecord | None:
    # 目的：查詢同病患同時段是否已有日常監測紀錄。
    # 為什麼：避免分開傳送或重送訊息時，產生重複入庫資料。
    day_start = recorded_at.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return (
        db.query(MonitoringRecord)
        .filter(
            MonitoringRecord.patient_id == patient.id,
            MonitoringRecord.record_type == record_type,
            MonitoringRecord.submitted_by_role == 'PATIENT',
            MonitoringRecord.recorded_at >= day_start,
            MonitoringRecord.recorded_at < day_end,
        )
        .order_by(MonitoringRecord.recorded_at.desc())
        .first()
    )


def _is_same_measurements(*, record: MonitoringRecord, measurements: dict) -> bool:
    stored_measurements = record.measurements if isinstance(record.measurements, dict) else {}
    comparable_fields = ('systolic', 'diastolic', 'weight_kg', 'blood_glucose_mg_dl')
    return all(stored_measurements.get(field_name) == measurements.get(field_name) for field_name in comparable_fields)


def _persist_monitoring_record(
    *,
    db: Session,
    patient: RenalPatient,
    record_type: str,
    recorded_at: datetime,
    measurements_payload: dict,
) -> tuple[bool, str]:
    # 目的：將完整監測欄位寫入 monitoring_records 並回傳回覆訊息。
    # 為什麼：將入庫與回覆文案集中在同一函式，降低分支重複與不一致風險。
    existing_record = _find_existing_patient_record(
        db=db,
        patient=patient,
        recorded_at=recorded_at,
        record_type=record_type,
    )
    if existing_record is not None and _is_same_measurements(record=existing_record, measurements=measurements_payload):
        systolic = int(measurements_payload.get('systolic') or 0)
        diastolic = int(measurements_payload.get('diastolic') or 0)
        weight_kg = float(measurements_payload.get('weight_kg') or 0)
        glucose_value = measurements_payload.get('blood_glucose_mg_dl')
        if glucose_value is not None:
            return True, f'今天同時段已紀錄你的血壓{systolic}/{diastolic}、體重 {weight_kg}kg、血糖 {glucose_value} mg/dL。'
        return True, f'今天同時段已紀錄你的血壓{systolic}/{diastolic}、體重 {weight_kg}kg。'

    try:
        renal_monitoring_service.create_monitoring_record(
            db=db,
            payload={
                'patient_id': str(patient.patient_code or ''),
                'record_type': record_type,
                'recorded_at': recorded_at.isoformat(),
                'submitted_by_role': 'PATIENT',
                'measurements': measurements_payload,
                'symptoms': {},
                'confirmed': True,
            },
        )
    except Exception as error:
        logger.warning('line_monitoring_write_failed', error=str(error)[:300])
        return False, '已收到你的量測資料，但系統寫入暫時失敗，請稍後再試一次。'

    systolic = int(measurements_payload.get('systolic') or 0)
    diastolic = int(measurements_payload.get('diastolic') or 0)
    weight_kg = float(measurements_payload.get('weight_kg') or 0)
    glucose_value = measurements_payload.get('blood_glucose_mg_dl')
    if glucose_value is not None:
        return True, f'收到你的血壓{systolic}/{diastolic}，體重 {weight_kg}kg，血糖 {glucose_value} mg/dL，紀錄完成。'
    return True, f'收到你的血壓{systolic}/{diastolic}，體重 {weight_kg}kg，紀錄完成。'


def _find_bound_patient_for_session(*, db: Session, session: LineChannelSession) -> RenalPatient | None:
    if session.bound_patient_id:
        return db.query(RenalPatient).filter(RenalPatient.id == session.bound_patient_id, RenalPatient.enabled == True).first()  # noqa: E712
    normalized_line_user_id = str(session.line_user_id or '').strip()
    if not normalized_line_user_id:
        return None
    return (
        db.query(RenalPatient)
        .filter(RenalPatient.line_user_id == normalized_line_user_id, RenalPatient.enabled == True)  # noqa: E712
        .first()
    )


async def _try_handle_monitoring_report_message(*, db: Session, session: LineChannelSession, inbound_text: str) -> tuple[bool, str | None]:
    # 目的：辨識 LINE 文字中的血壓/體重/血糖回報並寫入監測紀錄。
    # 為什麼：腎友回報屬高頻固定流程，需提供可預期的即時確認而非完全依賴 LLM 自由回覆。
    patient = _find_bound_patient_for_session(db=db, session=session)
    if patient is None:
        return False, None

    incoming_measurements = _build_measurements_from_text(inbound_text)
    if not incoming_measurements:
        return False, None

    recorded_at = datetime.now()
    record_type = _resolve_monitoring_record_type(recorded_at)
    record_date = recorded_at.date().isoformat()
    existing_draft = await _load_monitoring_draft(session=session, record_date=record_date, record_type=record_type)
    draft_measurements = existing_draft.get('measurements') if isinstance(existing_draft, dict) else {}
    merged_measurements = _merge_measurements(
        draft_measurements=draft_measurements,
        incoming_measurements=incoming_measurements,
    )
    missing_fields = _list_missing_measurements(patient=patient, measurements=merged_measurements)
    if missing_fields:
        await _save_monitoring_draft(
            session=session,
            record_date=record_date,
            record_type=record_type,
            measurements=merged_measurements,
        )
        return True, _format_missing_measurement_prompt(missing_fields=missing_fields)

    write_success, reply_text = _persist_monitoring_record(
        db=db,
        patient=patient,
        record_type=record_type,
        recorded_at=recorded_at,
        measurements_payload=merged_measurements,
    )
    if write_success:
        await _clear_monitoring_draft(session=session, record_date=record_date, record_type=record_type)
    return True, reply_text


async def _try_backfill_monitoring_record_from_agent_reply(
    *,
    db: Session,
    session: LineChannelSession,
    inbound_text: str,
    assistant_text: str,
) -> None:
    # 目的：當訊息已進入 Agent 回覆時，補做監測資料入庫保護。
    # 為什麼：避免 Agent 已明確回覆血壓/體重，但規則層遺漏導致未入庫。
    patient = _find_bound_patient_for_session(db=db, session=session)
    if patient is None:
        return
    combined_text = f'{str(inbound_text or "").strip()} {str(assistant_text or "").strip()}'.strip()
    if not combined_text:
        return

    merged_measurements = _build_measurements_from_text(combined_text)
    missing_fields = _list_missing_measurements(patient=patient, measurements=merged_measurements)
    if missing_fields:
        return

    recorded_at = datetime.now()
    record_type = _resolve_monitoring_record_type(recorded_at)
    write_success, _ = _persist_monitoring_record(
        db=db,
        patient=patient,
        record_type=record_type,
        recorded_at=recorded_at,
        measurements_payload=merged_measurements,
    )
    if write_success:
        record_date = recorded_at.date().isoformat()
        await _clear_monitoring_draft(session=session, record_date=record_date, record_type=record_type)


async def _reply_line_message(*, reply_token: str, text: str) -> None:
    # 目的：以 LINE reply API 回覆使用者訊息。
    # 為什麼：webhook 模式下 reply token 是即時回覆最穩定且成本最低的管道。
    channel_access_token = str(getattr(settings, 'LINE_CHANNEL_ACCESS_TOKEN', '') or '').strip()
    if not channel_access_token:
        raise validation_error('LINE_CHANNEL_ACCESS_TOKEN 尚未設定')
    payload = {
        'replyToken': str(reply_token or '').strip(),
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
        response = await client.post('https://api.line.me/v2/bot/message/reply', headers=headers, json=payload)
    if response.status_code >= 400:
        raise validation_error(f'LINE reply 失敗: {response.status_code} {response.text[:200]}')


async def _push_line_message(*, to_line_user_id: str, text: str) -> None:
    # 目的：以 LINE push API 主動發送訊息。
    # 為什麼：人工客服回覆不一定有可用 reply token，需要主動推送能力。
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
        'X-Line-Retry-Key': str(uuid.uuid4()),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post('https://api.line.me/v2/bot/message/push', headers=headers, json=payload)
    if response.status_code >= 400:
        raise validation_error(f'LINE push 失敗: {response.status_code} {response.text[:200]}')


@router.post('/line/webhook')
async def line_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_line_signature: str = Header(default=''),
):
    # 目的：統一處理 LINE webhook 事件（follow/message/unfollow）。
    # 為什麼：集中驗簽、去重與事件分派，避免重複邏輯散落在多個入口。
    raw_body = await request.body()
    if bool(getattr(settings, 'LINE_WEBHOOK_VERIFY_SIGNATURE', True)):
        if not _verify_line_signature(raw_body=raw_body, header_signature=x_line_signature):
            raise forbidden_error('LINE signature 驗證失敗')

    payload = await request.json()
    events = payload.get('events') if isinstance(payload, dict) else []
    processed = 0
    for event in (events if isinstance(events, list) else []):
        if not isinstance(event, dict):
            continue
        if await line_onboarding_service.should_skip_event(event=event):
            continue

        event_type = str(event.get('type') or '').strip()
        source_obj = event.get('source') if isinstance(event.get('source'), dict) else {}
        line_user_id = str(source_obj.get('userId') or '').strip()
        reply_token = str(event.get('replyToken') or '').strip()
        if not line_user_id:
            continue

        if event_type == 'follow':
            session, follow_reply = line_onboarding_service.handle_follow_event(db=db, line_user_id=line_user_id)
            should_send_welcome = await line_onboarding_service.should_send_follow_welcome(line_user_id=line_user_id)
            if follow_reply and should_send_welcome:
                _store_line_message(
                    db=db,
                    session=session,
                    direction='outbound',
                    sender_type='system',
                    content=follow_reply,
                )
                _sync_message_to_conversation(db=db, session=session, role='assistant', content=follow_reply)
                session.last_outbound_at = datetime.now()
                db.add(session)
                db.commit()
                db.refresh(session)
                if reply_token:
                    await _reply_line_message(reply_token=reply_token, text=follow_reply)
                else:
                    await _push_line_message(to_line_user_id=line_user_id, text=follow_reply)
            processed += 1
            continue

        if event_type == 'unfollow':
            line_onboarding_service.handle_unfollow_event(db=db, line_user_id=line_user_id)
            processed += 1
            continue

        if event_type != 'message':
            continue

        message_obj = event.get('message') if isinstance(event.get('message'), dict) else {}
        if str(message_obj.get('type') or '').strip() != 'text':
            continue
        inbound_text = str(message_obj.get('text') or '').strip()
        if not inbound_text:
            continue

        session = line_onboarding_service.get_or_create_line_session(db=db, line_user_id=line_user_id)
        session = line_onboarding_service.ensure_renal_companion_assignment(db=db, session=session)
        _store_line_message(
            db=db,
            session=session,
            direction='inbound',
            sender_type='user',
            content=inbound_text,
            line_message_id=str(message_obj.get('id') or '').strip() or None,
            reply_token=reply_token or None,
        )
        _sync_message_to_conversation(db=db, session=session, role='user', content=inbound_text)
        session.last_inbound_at = datetime.now()
        db.add(session)
        db.commit()
        db.refresh(session)

        binding_handled, binding_reply = line_onboarding_service.try_auto_bind_patient_from_dialog(
            db=db,
            session=session,
            inbound_text=inbound_text,
        )
        if binding_handled:
            if binding_reply:
                _store_line_message(
                    db=db,
                    session=session,
                    direction='outbound',
                    sender_type='system',
                    content=binding_reply,
                )
                _sync_message_to_conversation(db=db, session=session, role='assistant', content=binding_reply)
                session.last_outbound_at = datetime.now()
                db.add(session)
                db.commit()
                db.refresh(session)
                if reply_token:
                    await _reply_line_message(reply_token=reply_token, text=binding_reply)
            processed += 1
            continue

        monitoring_handled, monitoring_reply = await _try_handle_monitoring_report_message(
            db=db,
            session=session,
            inbound_text=inbound_text,
        )
        if monitoring_handled:
            if monitoring_reply:
                _store_line_message(
                    db=db,
                    session=session,
                    direction='outbound',
                    sender_type='system',
                    content=monitoring_reply,
                )
                _sync_message_to_conversation(db=db, session=session, role='assistant', content=monitoring_reply)
                session.last_outbound_at = datetime.now()
                db.add(session)
                db.commit()
                db.refresh(session)
                if reply_token:
                    await _reply_line_message(reply_token=reply_token, text=monitoring_reply)
                else:
                    await _push_line_message(to_line_user_id=line_user_id, text=monitoring_reply)
            processed += 1
            continue

        if str(session.mode or 'bot') == 'human':
            processed += 1
            continue

        assistant_text = _run_agent_reply(db=db, session=session, user_message=inbound_text)
        if assistant_text:
            await _try_backfill_monitoring_record_from_agent_reply(
                db=db,
                session=session,
                inbound_text=inbound_text,
                assistant_text=assistant_text,
            )
        if assistant_text:
            _store_line_message(
                db=db,
                session=session,
                direction='outbound',
                sender_type='agent',
                content=assistant_text,
            )
            _sync_message_to_conversation(db=db, session=session, role='assistant', content=assistant_text)
            session.last_outbound_at = datetime.now()
            db.add(session)
            db.commit()
            db.refresh(session)
            if reply_token:
                await _reply_line_message(reply_token=reply_token, text=assistant_text)
        processed += 1

    return {'ok': True, 'processed': processed}


@router.get('/line/sessions')
async def list_line_sessions(
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_line_operator_read_permission(current_user=current_user, db=db)
    safe_limit = max(1, min(int(limit or 50), 200))
    rows = db.query(LineChannelSession).order_by(LineChannelSession.updated_at.desc()).limit(safe_limit).all()
    output = []
    for row in rows:
        bound_summary = line_onboarding_service.build_bound_patient_summary(db=db, session=row)
        latest_message = db.query(LineMessage).filter(LineMessage.session_id == row.id).order_by(LineMessage.created_at.desc()).first()
        output.append(
            {
                'id': str(row.id),
                'line_user_id': str(row.line_user_id or ''),
                'conversation_id': str(row.conversation_id),
                'assigned_agent_id': str(row.assigned_agent_id) if row.assigned_agent_id else None,
                'mode': str(row.mode or 'bot'),
                'status': str(row.status or 'active'),
                'binding_status': str(row.binding_status or 'pending_name'),
                'binding_name': str(row.binding_name or ''),
                'binding_phone': str(row.binding_phone or ''),
                'bound_at': row.bound_at.isoformat() if row.bound_at else None,
                'bound_patient': bound_summary,
                'last_inbound_at': row.last_inbound_at.isoformat() if row.last_inbound_at else None,
                'last_outbound_at': row.last_outbound_at.isoformat() if row.last_outbound_at else None,
                'updated_at': row.updated_at.isoformat() if row.updated_at else None,
                'latest_message': {
                    'direction': str(getattr(latest_message, 'direction', '') or ''),
                    'sender_type': str(getattr(latest_message, 'sender_type', '') or ''),
                    'content': str(getattr(latest_message, 'content', '') or ''),
                    'created_at': latest_message.created_at.isoformat() if getattr(latest_message, 'created_at', None) else None,
                }
                if latest_message is not None
                else None,
            }
        )
    return {'sessions': output}


@router.get('/line/sessions/{session_id}/messages')
async def list_line_session_messages(
    session_id: str,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_line_operator_read_permission(current_user=current_user, db=db)
    try:
        uuid.UUID(str(session_id))
    except Exception as error:
        raise not_found_error('LineChannelSession', session_id) from error

    session = db.query(LineChannelSession).filter(LineChannelSession.id == session_id).first()
    if session is None:
        raise not_found_error('LineChannelSession', session_id)

    safe_limit = max(1, min(int(limit or 100), 500))
    rows = (
        db.query(LineMessage)
        .filter(LineMessage.session_id == session.id)
        .order_by(LineMessage.created_at.desc())
        .limit(safe_limit)
        .all()
    )
    rows.reverse()
    bound_summary = line_onboarding_service.build_bound_patient_summary(db=db, session=session)
    return {
        'session': {
            'id': str(session.id),
            'line_user_id': str(session.line_user_id),
            'conversation_id': str(session.conversation_id),
            'assigned_agent_id': str(session.assigned_agent_id) if session.assigned_agent_id else None,
            'mode': str(session.mode or 'bot'),
            'status': str(session.status or 'active'),
            'binding_status': str(session.binding_status or 'pending_name'),
            'binding_name': str(session.binding_name or ''),
            'binding_phone': str(session.binding_phone or ''),
            'bound_at': session.bound_at.isoformat() if session.bound_at else None,
            'bound_patient': bound_summary,
        },
        'messages': [
            {
                'id': str(row.id),
                'direction': str(row.direction or ''),
                'sender_type': str(row.sender_type or ''),
                'content': str(row.content or ''),
                'line_message_id': str(row.line_message_id or ''),
                'created_at': row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.put('/line/sessions/{session_id}/mode')
async def update_line_session_mode(
    session_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_line_operator_manage_permission(current_user=current_user, db=db)
    session = db.query(LineChannelSession).filter(LineChannelSession.id == session_id).first()
    if session is None:
        raise not_found_error('LineChannelSession', session_id)

    mode = str((payload or {}).get('mode') or '').strip().lower()
    if mode not in {'bot', 'human'}:
        raise validation_error('mode 僅支援 bot 或 human')
    session.mode = mode
    db.add(session)
    db.commit()
    db.refresh(session)
    return {'ok': True, 'id': str(session.id), 'mode': str(session.mode)}


@router.put('/line/sessions/{session_id}/agent')
async def update_line_session_agent(
    session_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_line_operator_manage_permission(current_user=current_user, db=db)
    session = db.query(LineChannelSession).filter(LineChannelSession.id == session_id).first()
    if session is None:
        raise not_found_error('LineChannelSession', session_id)

    agent_id = str((payload or {}).get('agent_id') or '').strip()
    if not agent_id:
        raise validation_error('agent_id 不可為空')
    agent = db.query(Agent).filter(Agent.id == agent_id, Agent.enabled == True).first()  # noqa: E712
    if agent is None:
        raise validation_error('agent_id 無效或代理者未啟用')
    session.assigned_agent_id = agent.id
    db.add(session)
    db.commit()
    db.refresh(session)
    return {'ok': True, 'id': str(session.id), 'assigned_agent_id': str(session.assigned_agent_id)}


@router.post('/line/sessions/{session_id}/messages')
async def send_line_session_message(
    session_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_line_operator_manage_permission(current_user=current_user, db=db)
    session = db.query(LineChannelSession).filter(LineChannelSession.id == session_id).first()
    if session is None:
        raise not_found_error('LineChannelSession', session_id)

    message_text = str((payload or {}).get('text') or '').strip()
    if not message_text:
        raise validation_error('text 不可為空')

    await _push_line_message(to_line_user_id=str(session.line_user_id), text=message_text)

    operator_label = str(getattr(current_user, 'username', '') or '').strip() or str(current_user.id)
    formatted_text = f'[人工客服:{operator_label}] {message_text}'
    _store_line_message(
        db=db,
        session=session,
        direction='outbound',
        sender_type='operator',
        content=message_text,
        operator_user_id=str(current_user.id),
    )
    _sync_message_to_conversation(db=db, session=session, role='assistant', content=formatted_text)
    session.last_outbound_at = datetime.now()
    db.add(session)
    db.commit()
    db.refresh(session)
    return {'ok': True, 'session_id': str(session.id), 'sent': True}
