from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Body, Depends, Header, Request
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error, not_found_error, validation_error
from src.core.config import settings
from src.core.database import get_db
from src.core.logging import get_logger
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import Agent, Conversation, LineChannelSession, LineMessage, Message, User
from src.services.chat_router import ChatRouter

router = APIRouter()
logger = get_logger(__name__)


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
    # 為什麼：LINE 對話可能含個資，需至少具備聊天權限才可檢視。
    if not check_permission(current_user, 'chat', db=db):
        raise forbidden_error('無權限檢視 LINE 對話')


def _require_line_operator_manage_permission(*, current_user: User, db: Session) -> None:
    # 目的：限制 LINE 對話操作（人工回覆、切換模式）權限。
    # 為什麼：避免一般使用者誤發外部訊息給客戶。
    can_manage = bool(
        check_permission(current_user, 'update_agent', db=db)
        or check_permission(current_user, 'read_logs', db=db)
    )
    if not can_manage:
        raise forbidden_error('無權限操作 LINE 對話')


def _pick_default_agent(db: Session) -> Agent:
    # 目的：解析 LINE 對話預設使用的代理者。
    # 為什麼：LINE 入口需有穩定 fallback，避免 webhook 事件因未綁定代理而失敗。
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


def _get_or_create_line_session(*, db: Session, line_user_id: str) -> LineChannelSession:
    # 目的：查找或建立 LINE 對話 Session。
    # 為什麼：需維持同一 LINE 使用者的會話連續性與模式狀態。
    normalized_line_user_id = str(line_user_id or '').strip()
    if not normalized_line_user_id:
        raise validation_error('line_user_id 不可為空')

    session = db.query(LineChannelSession).filter(LineChannelSession.line_user_id == normalized_line_user_id).first()
    if session is not None:
        return session

    target_agent = _pick_default_agent(db)
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
        mode='bot',
        status='active',
        last_inbound_at=datetime.now(),
        last_outbound_at=None,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


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
        target_agent = _pick_default_agent(db)
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
        event_type = str(event.get('type') or '').strip()
        if event_type != 'message':
            continue
        message_obj = event.get('message') if isinstance(event.get('message'), dict) else {}
        if str(message_obj.get('type') or '').strip() != 'text':
            continue

        source_obj = event.get('source') if isinstance(event.get('source'), dict) else {}
        line_user_id = str(source_obj.get('userId') or '').strip()
        reply_token = str(event.get('replyToken') or '').strip()
        inbound_text = str(message_obj.get('text') or '').strip()
        if (not line_user_id) or (not inbound_text):
            continue

        session = _get_or_create_line_session(db=db, line_user_id=line_user_id)
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

        if str(session.mode or 'bot') == 'human':
            processed += 1
            continue

        assistant_text = _run_agent_reply(db=db, session=session, user_message=inbound_text)
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
        latest_message = db.query(LineMessage).filter(LineMessage.session_id == row.id).order_by(LineMessage.created_at.desc()).first()
        output.append(
            {
                'id': str(row.id),
                'line_user_id': str(row.line_user_id or ''),
                'conversation_id': str(row.conversation_id),
                'assigned_agent_id': str(row.assigned_agent_id) if row.assigned_agent_id else None,
                'mode': str(row.mode or 'bot'),
                'status': str(row.status or 'active'),
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
    return {
        'session': {
            'id': str(session.id),
            'line_user_id': str(session.line_user_id),
            'conversation_id': str(session.conversation_id),
            'assigned_agent_id': str(session.assigned_agent_id) if session.assigned_agent_id else None,
            'mode': str(session.mode or 'bot'),
            'status': str(session.status or 'active'),
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
