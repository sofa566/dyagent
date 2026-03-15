from fastapi import APIRouter, Depends, Body
from typing import Any
import uuid
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
import json as _json
import time

from src.core.database import get_db
from src.models import User, Agent, Conversation, Message
from src.models.events import EventPart
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error, forbidden_error
from src.services.chat_router import ChatRouter
from src.core.logging import get_logger
from src.api.errors import service_unavailable_error

router = APIRouter()
_log = get_logger("api.chat")


class ChatResponse(BaseModel):
    response: str
    conversation_id: str


@router.post('/agents/{agent_id}/chat', response_model=ChatResponse)
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

    try:
        response_content = router.single_turn(
            session_id=str(conversation.id),
            agent_id=str(agent.id),
            user_message=message,
            tier=overrides.get('tier'),  # 若 Agent 指定 tier 則覆蓋
            db=db,      # 傳入 DB 以便事件/審計寫盤
            llm_overrides=overrides,
        )
    except RuntimeError as e:
        # 若啟用硬失敗且沒有可用模型路由，回傳 503 並不寫入助理訊息
        if str(e) == 'no_route':
            raise service_unavailable_error('模型路由不可用，請檢查金鑰或端點設定')
        raise
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

    # 強制轉為字串以避免 None 導致的 JSON null
    return ChatResponse(response=str(response_content or ''), conversation_id=str(conversation.id))


@router.post('/conversations/{conversation_id}/tools/{tool_name}')
async def invoke_tool(
    conversation_id: str,
    tool_name: str,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'chat'):
        raise forbidden_error()

    try:
        uuid.UUID(str(conversation_id))
    except ValueError:
        raise not_found_error('Conversation', conversation_id)

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise not_found_error('Conversation', conversation_id)

    agent = db.query(Agent).filter(Agent.id == conv.agent_id).first()
    if agent is None:
        raise not_found_error('Agent', str(conv.agent_id))

    router = ChatRouter()
    # 工具呼叫：優先走 WS 串流（若不可用則回退 HTTP），並強制白名單
    result = await router.call_tool_async(session_id=str(conv.id), tool=tool_name, payload=payload or {}, db=db, agent_id=str(agent.id))
    return result


@router.get('/conversations/{conversation_id}/tools/{tool_name}/stream')
async def stream_tool(
    conversation_id: str,
    tool_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """以 SSE 方式串流工具輸出。優先走 MCP WS JSON-RPC；若不可用則回退單段 HTTP 結果。

    備註：目前為最小骨架；前端可用 EventSource 訂閱。
    """
    if not check_permission(current_user, 'chat'):
        raise forbidden_error()

    try:
        uuid.UUID(str(conversation_id))
    except ValueError:
        raise not_found_error('Conversation', conversation_id)

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise not_found_error('Conversation', conversation_id)

    agent = db.query(Agent).filter(Agent.id == conv.agent_id).first()
    if agent is None:
        raise not_found_error('Agent', str(conv.agent_id))

    router = ChatRouter()

    async def _gen():
        # 準備白名單/連線映射
        router._prepare_integrations(db=db, agent_id=str(agent.id))
        # 僅對 mcp:* 嘗試 WS 串流；其他工具回單段
        if tool_name.startswith('mcp:') and hasattr(router, '_mcp'):
            conn_name = tool_name.split(':', 1)[1]
            conn = getattr(router, '_mcp_map', {}).get(conn_name)
            if conn and conn.get('base_url'):
                base_url = str(conn.get('base_url') or '').strip()
                auth = conn.get('auth') if isinstance(conn, dict) else None
                # 讀取對映設定
                progress_key = (conn.get('progress_field') or '').strip() if isinstance(conn, dict) else ''
                eta_key = (conn.get('eta_field') or '').strip() if isinstance(conn, dict) else ''
                def _get_by_path(obj: dict, path: str):
                    try:
                        if not path:
                            return None
                        cur = obj
                        for part in path.split('.'):
                            if isinstance(cur, dict) and part in cur:
                                cur = cur[part]
                            else:
                                return None
                        return cur
                    except Exception:
                        return None
                last_prog_ts = 0.0
                last_prog_val: float | None = None
                async for frame in router._mcp.stream_rpc_call_ws(
                    base_url=base_url,
                    method='tools.invoke',
                    params={'tool': conn_name, 'arguments': {}},
                    auth=auth if isinstance(auth, dict) else None,
                ):
                    # 標準化進度事件：value (0..100), eta_seconds
                    try:
                        if isinstance(frame, dict):
                            # 先依 Agent 設定對映欄位
                            val = _get_by_path(frame, progress_key) if progress_key else None
                            if not isinstance(val, (int, float)):
                                # 回退：常見欄位
                                if isinstance(frame.get('progress'), (int, float)):
                                    val = frame.get('progress')
                                elif isinstance(frame.get('percent'), (int, float)):
                                    val = frame.get('percent')
                                elif isinstance(frame.get('value'), (int, float)):
                                    val = frame.get('value')
                            if val is not None:
                                # ETA 對映或回退
                                eta = _get_by_path(frame, eta_key) if eta_key else None
                                if not isinstance(eta, (int, float)):
                                    if isinstance(frame.get('eta_seconds'), (int, float)):
                                        eta = frame.get('eta_seconds')
                                    elif isinstance(frame.get('eta'), (int, float)):
                                        eta = frame.get('eta')
                                    elif isinstance(frame.get('remaining_ms'), (int, float)):
                                        rem_ms = frame.get('remaining_ms')
                                        eta = (rem_ms / 1000.0) if isinstance(rem_ms, (int, float)) else None
                                # 節流：僅在 >=200ms 或進度變化>=1% 才推送
                                now = time.monotonic()
                                fval = float(val)
                                should_emit = (now - last_prog_ts >= 0.2) or (last_prog_val is None) or (abs(fval - last_prog_val) >= 1.0)
                                if should_emit:
                                    last_prog_ts = now
                                    last_prog_val = fval
                                    prog_payload = { 'type': 'progress', 'name': tool_name, 'value': fval }
                                    if isinstance(eta, (int, float)):
                                        prog_payload['eta_seconds'] = float(eta)
                                    yield f"data: {_json.dumps(prog_payload, ensure_ascii=False)}\n\n"
                    except Exception:
                        pass
                    yield f"data: {_json.dumps(frame, ensure_ascii=False)}\n\n"
                return
        # 回退：單段結果
        res = await router.call_tool_async(session_id=str(conv.id), tool=tool_name, payload={}, db=db, agent_id=str(agent.id))
        yield f"data: {_json.dumps(res, ensure_ascii=False)}\n\n"

    return StreamingResponse(_gen(), media_type='text/event-stream')


@router.get('/agents/{agent_id}/chat/stream')
async def chat_stream(
    agent_id: str,
    message: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """SSE 串流聊天：以 LLM 串流文字增量，遇到工具呼叫（[[CALL tool=...]]+JSON）即時串流工具結果。"""
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    if not message or len(message.strip()) == 0:
        raise validation_error('Message cannot be empty')

    # 會話與 user 訊息（與同步端點一致）
    try:
        conversation = db.query(Conversation).filter(
            Conversation.agent_id == agent_id,
            (Conversation.user_id == current_user.id)
        ).order_by(Conversation.last_interacted_at.desc()).first()
    except (ProgrammingError, OperationalError):
        conversation = db.query(Conversation).filter(
            Conversation.agent_id == agent_id
        ).order_by(Conversation.created_at.desc()).first()
    if not conversation:
        try:
            conversation = Conversation(agent_id=agent_id, user_id=current_user.id)
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
        except (ProgrammingError, OperationalError):
            db.rollback()
            conversation = Conversation(agent_id=agent_id)
            db.add(conversation)
            db.commit()
            db.refresh(conversation)

    user_message = Message(conversation_id=conversation.id, role='user', content=message)
    db.add(user_message)
    try:
        from datetime import datetime as _dt
        setattr(conversation, 'last_interacted_at', _dt.utcnow())
        db.commit()
    except (ProgrammingError, OperationalError):
        db.rollback()
        db.add(user_message)
        db.commit()

    router = ChatRouter()
    # 準備 overrides
    overrides: dict[str, Any] = {}
    try:
        cfg = agent.model_config or {}
        if isinstance(cfg, dict):
            # 若自訂工具呼叫指引存在，帶入 Router
            if isinstance(cfg.get('toolcall_guide'), str) and cfg.get('toolcall_guide').strip():
                router.set_toolcall_guide(cfg.get('toolcall_guide'))
            agent_model_type = str(agent.model_type) if getattr(agent, 'model_type', None) is not None else ''
            if agent_model_type == 'local' and not bool(cfg.get('tier')):
                overrides['tier'] = 'onprem'
            if agent_model_type == 'cloud' and not bool(cfg.get('tier')):
                overrides['tier'] = 'cloud'
            for k in (
                'tier','provider','model','base_url','onprem_provider','onprem_base_url','api_key','api_key_ref',
                'azure_endpoint','azure_api_version','azure_deployment','azure_api_key_ref'):
                v = cfg.get(k)
                if v:
                    overrides[k] = v
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

    try:
        router._llm.init_for_session(session_id=str(conversation.id), preferred_tier=overrides.get('tier'), overrides=overrides)
    except TypeError:
        router._llm.init_for_session(session_id=str(conversation.id), preferred_tier=overrides.get('tier'))

    # 設定工具呼叫協定模板（若代理者有自訂）
    try:
        cfg = agent.model_config or {}
        if isinstance(cfg, dict) and isinstance(cfg.get('toolcall_guide'), str) and cfg.get('toolcall_guide').strip():
            router.set_toolcall_guide(cfg.get('toolcall_guide'))
    except Exception:
        pass

    agent_ctx = router._prepare_integrations(db=db, agent_id=str(agent.id))
    mcp_names = ",".join([str(c.get("name") or "").strip() for c in agent_ctx.get("mcp", []) if isinstance(c, dict) and c.get("enabled")])
    skills_names = ",".join(agent_ctx.get("skills", []))
    rag = agent_ctx.get('rag', {}) or {}
    rag_prefix = f"\n[RAG] sources={','.join(rag.get('sources', []) or [])} topK={int(rag.get('topK', 5) or 5)}" if rag.get('enabled') else ''
    prefix = f"[Agent Capabilities] skills={skills_names} mcp={mcp_names}{rag_prefix}\n"
    guide = router._toolcall_guide()
    composed_user_message = f"{prefix}{guide}\n{message}"

    async def _gen():
        buffer = ''
        detected_tool = False
        tool_name = ''
        tool_payload: dict[str, Any] = {}
        for delta in router._llm.stream_complete(prompt=composed_user_message, tier=overrides.get('tier')):
            if not isinstance(delta, str):
                continue
            buffer += delta
            if not detected_tool:
                mk = '[[CALL tool='
                p = buffer.find(mk)
                if p >= 0:
                    tail = buffer[p + len(mk):]
                    end = tail.find(']]')
                    if end >= 0:
                        tool_name = tail[:end].strip()
                        after = tail[end+2:].lstrip()
                        js = after.find('{')
                        je = after.rfind('}')
                        if js >= 0 and je >= 0 and je > js:
                            try:
                                tool_payload = _json.loads(after[js:je+1])
                            except Exception:
                                tool_payload = {}
                        detected_tool = True
            # 推送文字增量
            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(delta, ensure_ascii=False)} }}\n\n"

            if detected_tool:
                detected_tool = False
                # 通知前端工具開始（附上參數以支援前端重執行）
                start_payload = {"type": "tool_start", "name": tool_name, "args": tool_payload or {}}
                yield f"data: {_json.dumps(start_payload, ensure_ascii=False)}\n\n"
                if tool_name.startswith('mcp:') and hasattr(router, '_mcp'):
                    conn = getattr(router, '_mcp_map', {}).get(tool_name.split(':',1)[1])
                    if conn and conn.get('base_url'):
                        base_url = str(conn.get('base_url') or '').strip()
                        auth = conn.get('auth') if isinstance(conn, dict) else None
                        try:
                            async for frame in router._mcp.stream_rpc_call_ws(
                                base_url=base_url,
                                method='tools.invoke',
                                params={'tool': tool_name.split(':',1)[1], 'arguments': tool_payload or {}},
                                auth=auth if isinstance(auth, dict) else None,
                            ):
                                # 標準化進度事件
                                try:
                                    if isinstance(frame, dict):
                                        val = None
                                        if isinstance(frame.get('progress'), (int, float)):
                                            val = frame.get('progress')
                                        elif isinstance(frame.get('percent'), (int, float)):
                                            val = frame.get('percent')
                                        elif isinstance(frame.get('value'), (int, float)):
                                            val = frame.get('value')
                                        if val is not None:
                                            eta = None
                                            if isinstance(frame.get('eta_seconds'), (int, float)):
                                                eta = frame.get('eta_seconds')
                                            elif isinstance(frame.get('eta'), (int, float)):
                                                eta = frame.get('eta')
                                            elif isinstance(frame.get('remaining_ms'), (int, float)):
                                                eta = frame.get('remaining_ms') / 1000.0
                                            prog_payload = { 'type': 'progress', 'name': tool_name, 'value': float(val) }
                                            if eta is not None:
                                                prog_payload['eta_seconds'] = float(eta)
                                            yield f"data: {_json.dumps(prog_payload, ensure_ascii=False)}\n\n"
                                except Exception:
                                    pass
                                yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(frame, ensure_ascii=False)} }}\n\n"
                        except Exception:
                            res = await router.call_tool_async(session_id=str(conversation.id), tool=tool_name, payload=tool_payload or {}, db=db, agent_id=str(agent.id))
                            yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(res, ensure_ascii=False)} }}\n\n"
                else:
                    res = await router.call_tool_async(session_id=str(conversation.id), tool=tool_name, payload=tool_payload or {}, db=db, agent_id=str(agent.id))
                    yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(res, ensure_ascii=False)} }}\n\n"

        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"

    return StreamingResponse(_gen(), media_type='text/event-stream')


@router.get('/conversations/{conversation_id}/events')
async def list_conversation_events(
    conversation_id: str,
    types: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'chat'):
        raise forbidden_error()

    try:
        uuid.UUID(str(conversation_id))
    except ValueError:
        raise not_found_error('Conversation', conversation_id)

    q = db.query(EventPart).filter(EventPart.conversation_id == conversation_id)
    type_list = None
    if types:
        type_list = [t.strip() for t in types.split(',') if t.strip()]
        if type_list:
            q = q.filter(EventPart.type.in_(type_list))
    rows = q.order_by(EventPart.created_at.asc()).all()
    def _to_dict(e: EventPart):
        try:
            return {
                'id': str(e.id),
                'type': e.type,
                'payload': e.payload,
                'created_at': e.created_at.isoformat() if e.created_at else None,
            }
        except Exception:
            return {'id': None, 'type': None, 'payload': None, 'created_at': None}
    return {'events': [_to_dict(r) for r in rows]}


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


@router.post('/agents/{agent_id}/conversations')
async def create_conversation(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """建立一個新的會話（屬於目前使用者與指定代理者）。"""
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 嘗試以 user_id 欄位建立；若資料庫尚未遷移，回退為無 user_id 建立
    try:
        conv = Conversation(agent_id=agent_id, user_id=current_user.id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.user_id", error=str(e))
        db.rollback()
        conv = Conversation(agent_id=agent_id)
        db.add(conv)
        db.commit()
        db.refresh(conv)

    _li = getattr(conv, 'last_interacted_at', None)
    return {
        'id': str(conv.id),
        'agent_id': str(conv.agent_id),
        'title': getattr(conv, 'title', None) or '',
        'messages': [],
        'created_at': conv.created_at.isoformat() if (conv.created_at is not None) else None,
        'last_interacted_at': _li.isoformat() if _li is not None else None,
    }


@router.put('/conversations/{conversation_id}/title')
async def rename_conversation(
    conversation_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重新命名一個會話的標題（僅限目前使用者擁有的會話）。"""
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(conversation_id))
    except ValueError:
        raise not_found_error('Conversation', conversation_id)

    title = (payload or {}).get('title') if isinstance(payload, dict) else None
    if not isinstance(title, str) or not title.strip():
        raise validation_error('標題不可為空')
    title = title.strip()
    if len(title) > 200:
        raise validation_error('標題長度不可超過 200 字元')

    # 僅允許擁有者重新命名
    try:
        conv = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            (Conversation.user_id == current_user.id)
        ).first()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.user_id/title", error=str(e))
        # 若無 user_id 欄位，退回只比對 id（風險：在未遷移期間不做擁有者檢查）
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()

    if not conv:
        raise not_found_error('Conversation', conversation_id)

    # 嘗試更新標題；若 title 欄位不存在，回報友善錯誤
    try:
        setattr(conv, 'title', title)
        db.commit()
        db.refresh(conv)
    except (ProgrammingError, OperationalError):
        db.rollback()
        raise validation_error('系統尚未啟用會話標題欄位，請聯繫管理員更新資料庫')

    _li2 = getattr(conv, 'last_interacted_at', None)
    return {
        'ok': True,
        'conversation': {
            'id': str(conv.id),
            'agent_id': str(conv.agent_id),
            'title': getattr(conv, 'title', None) or '',
            'created_at': conv.created_at.isoformat() if (conv.created_at is not None) else None,
            'last_interacted_at': _li2.isoformat() if _li2 is not None else None,
        }
    }
