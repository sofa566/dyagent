from fastapi import APIRouter, Depends
from typing import Any
import uuid
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError

from src.core.database import get_db
from src.models import User, Agent, Conversation, Message
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error
from src.services.chat_router import ChatRouter
from src.core.logging import get_logger

router = APIRouter()
_log = get_logger("api.chat")


@router.post('/agents/{agent_id}/chat')
async def chat_with_agent(
    agent_id: str,
    message: str,
    format: str | None = None,
    schema: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    # Validate UUID to avoid DB binding errors
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    if not message or len(message.strip()) == 0:
        raise validation_error('Message cannot be empty')

    # 嘗試以新欄位（user_id / last_interacted_at）查詢；若資料庫尚未遷移，退回舊邏輯
    try:
        conversation = db.query(Conversation).filter(
            Conversation.agent_id == agent_id,
            (Conversation.user_id == current_user.id)
        ).order_by(Conversation.last_interacted_at.desc()).first()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.user_id/last_interacted_at", error=str(e))
        conversation = db.query(Conversation).filter(
            Conversation.agent_id == agent_id
        ).order_by(Conversation.created_at.desc()).first()

    if not conversation:
        try:
            conversation = Conversation(agent_id=agent_id, user_id=current_user.id)
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        except (ProgrammingError, OperationalError) as e:
            _log.warning("db.migration.missing_columns", hint="conversations.user_id", error=str(e))
            # 退回不含 user_id 的建立，確保功能不中斷
            db.rollback()
            conversation = Conversation(agent_id=agent_id)
            db.add(conversation)
            db.commit()
            db.refresh(conversation)

    user_message = Message(
        conversation_id=conversation.id,
        role='user',
        content=message,
    )
    db.add(user_message)
    # 更新最近互動時間（若欄位存在）
    try:
        from datetime import datetime as _dt
        setattr(conversation, 'last_interacted_at', _dt.utcnow())
        db.commit()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.last_interacted_at", error=str(e))
        db.rollback()
        # 退回：僅提交訊息
        db.add(user_message)
        db.commit()

    # 使用 ChatRouter 執行單輪回合以產生助理回覆
    router = ChatRouter()
    # 若指定 json_schema，啟用結構化輸出模式（可選 schema，無法解析則使用空物件）
    if format and format.lower() == 'json_schema':
        import json
        try:
            parsed = json.loads(schema) if schema else {}
        except Exception:
            parsed = {}
        router.enable_structured_output(schema=parsed)
    # 構建每-Agent LLM 覆蓋設定（允許多代理者/多供應商並行）
    overrides: dict[str, Any] = {}
    try:
        cfg = agent.model_config or {}
        if isinstance(cfg, dict):
            # 支援的鍵：tier/provider/model/base_url/onprem_provider/onprem_base_url/api_key/api_key_ref
            agent_model_type = str(agent.model_type) if getattr(agent, 'model_type', None) is not None else ''
            if agent_model_type == 'local' and not bool(cfg.get('tier')):
                overrides['tier'] = 'onprem'
            if agent_model_type == 'cloud' and not bool(cfg.get('tier')):
                overrides['tier'] = 'cloud'
            for k in (
                'tier','provider','model','base_url','onprem_provider','onprem_base_url','api_key','api_key_ref',
                'azure_endpoint','azure_api_version','azure_deployment','azure_api_key_ref'
            ):
                v = cfg.get(k)
                if v:
                    overrides[k] = v
            # 相容舊鍵名：onprem.base_url / cloud.provider 等嵌套
            if not overrides.get('onprem_base_url') and isinstance(cfg.get('onprem'), dict):
                v = cfg.get('onprem', {}).get('base_url')
                if v:
                    overrides['onprem_base_url'] = v
            if not overrides.get('provider') and isinstance(cfg.get('cloud'), dict):
                v = cfg.get('cloud', {}).get('provider')
                if v:
                    overrides['provider'] = v
            if not overrides.get('model') and isinstance(cfg.get('cloud'), dict):
                v = cfg.get('cloud', {}).get('model')
                if v:
                    overrides['model'] = v
    except Exception:
        overrides = {}

    response_content = router.single_turn(
        session_id=str(conversation.id),
        agent_id=str(agent.id),
        user_message=message,
        tier=overrides.get('tier'),  # 若 Agent 指定 tier 則覆蓋
        db=db,      # 傳入 DB 以便事件/審計寫盤
        llm_overrides=overrides,
    )

    assistant_message = Message(
        conversation_id=conversation.id,
        role='assistant',
        content=response_content,
    )
    db.add(assistant_message)
    try:
        from datetime import datetime as _dt
        setattr(conversation, 'last_interacted_at', _dt.utcnow())
        db.commit()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.last_interacted_at", error=str(e))
        db.rollback()
        # 退回：僅提交訊息
        db.add(assistant_message)
        db.commit()

    resp: dict[str, Any] = {
        'response': response_content,
        'conversation_id': str(conversation.id),
    }
    # 若為結構化輸出模式，回傳 structured 欄位（文本回覆預期可為空字串）
    if format and format.lower() == 'json_schema':
        resp['structured'] = router.last_structured() or {}
    return resp


@router.get('/agents/{agent_id}/conversations')
async def get_conversations(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    # Validate UUID to avoid DB binding errors
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    try:
        conversations = db.query(Conversation).filter(
            Conversation.agent_id == agent_id,
            (Conversation.user_id == current_user.id)
        ).order_by(Conversation.last_interacted_at.desc()).all()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.user_id/last_interacted_at", error=str(e))
        conversations = db.query(Conversation).filter(
            Conversation.agent_id == agent_id
        ).order_by(Conversation.created_at.desc()).all()

    result = []
    for conv in conversations:
        messages = db.query(Message).filter(
            Message.conversation_id == conv.id
        ).order_by(Message.timestamp).all()

        result.append({
            'id': str(conv.id),
            'agent_id': str(conv.agent_id),
            'title': (conv.title or ''),
            'messages': [
                {
                    'id': str(m.id),
                    'role': m.role,
                    'content': m.content,
                    'timestamp': m.timestamp.isoformat() if (m.timestamp is not None) else None,
                }
                for m in messages
            ],
            'created_at': conv.created_at.isoformat() if (conv.created_at is not None) else None,
            'last_interacted_at': conv.last_interacted_at.isoformat() if getattr(conv, 'last_interacted_at', None) else None,
        })

    return {'conversations': result}
