from fastapi import APIRouter, Depends, Body
from typing import Any
import uuid
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError, OperationalError
from fastapi.responses import StreamingResponse
import json as _json
import time
import asyncio
import threading
import re
from functools import lru_cache
from decimal import Decimal
from datetime import datetime

from src.core.database import get_db
from src.models import User, Agent, Conversation, Message, LlmTurn
from src.models.events import EventPart
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error, forbidden_error
from src.services.chat_router import ChatRouter
from src.core.logging import get_logger
from src.core.config import settings

router = APIRouter()
_log = get_logger("api.chat")


async def _stream_complete_async(llm_client: Any, *, prompt: str, tier: str | None):
    """將同步 LLM 串流橋接為非同步迭代，避免阻塞 event loop。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue(maxsize=256)

    def _producer() -> None:
        try:
            for delta in llm_client.stream_complete(prompt=prompt, tier=tier):
                asyncio.run_coroutine_threadsafe(queue.put(("delta", str(delta))), loop).result()
        except Exception as e:
            asyncio.run_coroutine_threadsafe(queue.put(("error", str(e))), loop).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(("done", None)), loop).result()

    t = threading.Thread(target=_producer, daemon=True)
    t.start()

    while True:
        typ, payload = await queue.get()
        if typ == "delta":
            if payload is not None:
                yield payload
            continue
        if typ == "error":
            raise RuntimeError(payload or "stream_error")
        break


def _require_chat_permission(current_user: User) -> None:
    """目的：集中聊天權限檢查。
    為什麼：避免多個路由重複寫同一檢查邏輯，降低遺漏風險。
    """
    if not check_permission(current_user, 'chat'):
        raise forbidden_error()


def _validate_uuid_or_not_found(entity_name: str, raw_value: str) -> None:
    """目的：統一 UUID 驗證與錯誤型別。
    為什麼：保持所有端點對無效 ID 的回應一致。
    """
    try:
        uuid.UUID(str(raw_value))
    except ValueError:
        raise not_found_error(entity_name, raw_value)


def _query_latest_conversation_with_fallback(*, db: Session, agent_id: str, user_id: Any) -> Conversation | None:
    """目的：查詢最近會話並兼容舊資料庫欄位。
    為什麼：在資料庫尚未遷移完成時，仍維持聊天可用。
    """
    try:
        return db.query(Conversation).filter(
            Conversation.agent_id == agent_id,
            (Conversation.user_id == user_id),
        ).order_by(Conversation.last_interacted_at.desc()).first()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.user_id/last_interacted_at", error=str(e))
        return db.query(Conversation).filter(
            Conversation.agent_id == agent_id,
        ).order_by(Conversation.created_at.desc()).first()


def _create_conversation_with_fallback(*, db: Session, agent_id: str, user_id: Any) -> Conversation:
    """目的：建立會話並兼容無 user_id 欄位的舊資料庫。
    為什麼：確保新舊 schema 都能建立會話，不中斷服務。
    """
    try:
        conversation = Conversation(agent_id=agent_id, user_id=user_id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.user_id", error=str(e))
        db.rollback()
        conversation = Conversation(agent_id=agent_id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        return conversation


def _save_message_with_touch_fallback(*, db: Session, conversation: Conversation, message_obj: Message) -> None:
    """目的：儲存訊息並更新最後互動時間。
    為什麼：last_interacted_at 在舊版 schema 可能不存在，需保留回退提交路徑。
    """
    db.add(message_obj)
    try:
        setattr(conversation, 'last_interacted_at', datetime.utcnow())
        db.commit()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.last_interacted_at", error=str(e))
        db.rollback()
        db.add(message_obj)
        db.commit()


def _build_agent_overrides(agent: Agent) -> dict[str, Any]:
    """目的：集中 Agent LLM 覆寫參數解析。
    為什麼：同步與串流端點共用同一規則，避免配置漂移。
    """
    overrides: dict[str, Any] = {}
    try:
        cfg = agent.model_config or {}
        if isinstance(cfg, dict):
            agent_model_type = str(agent.model_type) if getattr(agent, 'model_type', None) is not None else ''
            if agent_model_type == 'local' and not bool(cfg.get('tier')):
                overrides['tier'] = 'onprem'
            if agent_model_type == 'cloud' and not bool(cfg.get('tier')):
                overrides['tier'] = 'cloud'
            for k in (
                'tier', 'provider', 'model', 'base_url', 'onprem_provider', 'onprem_base_url', 'api_key', 'api_key_ref',
                'azure_endpoint', 'azure_api_version', 'azure_deployment', 'azure_api_key_ref',
            ):
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
    return overrides


def _apply_custom_toolcall_guide(router_obj: ChatRouter, agent: Agent) -> None:
    """目的：套用代理者自訂工具呼叫指引。
    為什麼：讓指引套用邏輯單點維護，避免多處重複 try/except。
    """
    try:
        cfg = agent.model_config or {}
        if isinstance(cfg, dict):
            guide = cfg.get('functions_definition_template')
            if not (isinstance(guide, str) and guide.strip()):
                guide = cfg.get('toolcall_guide')
            if isinstance(guide, str) and guide.strip():
                router_obj.set_toolcall_guide(guide)
    except Exception:
        pass


def _build_capability_prompt(*, router_obj: ChatRouter, agent_ctx: dict[str, Any], message: str) -> str:
    """目的：統一能力前綴與工具指引拼接。
    為什麼：避免不同路由在 prompt 組裝上出現不一致。
    """
    mcp_names = ",".join([
        str(c.get("name") or "").strip() for c in agent_ctx.get("mcp", [])
        if isinstance(c, dict) and c.get("enabled")
    ])
    skills_names = ",".join(agent_ctx.get("skills", []))
    rag = agent_ctx.get('rag', {}) or {}
    rag_prefix = f"\n[RAG] sources={','.join(rag.get('sources', []) or [])} topK={int(rag.get('topK', 5) or 5)}" if rag.get('enabled') else ''
    prefix = f"[Agent Capabilities] skills={skills_names} mcp={mcp_names}{rag_prefix}\n"
    guide = router_obj._render_toolcall_guide(agent_ctx)
    return f"{prefix}{guide}\n{message}"


def _resolve_agent_system_prompt_snapshot(agent: Agent) -> str:
    """目的：取得本輪使用的 system prompt 快照。
    為什麼：llm_turns 需要保留審計資料，供後續問題追溯。
    """
    if isinstance(getattr(agent, 'system_prompt', None), str) and agent.system_prompt.strip():
        return agent.system_prompt.strip()
    cfg = agent.model_config if isinstance(agent.model_config, dict) else {}
    cfg_prompt = cfg.get('system_prompt') if isinstance(cfg, dict) else None
    if isinstance(cfg_prompt, str) and cfg_prompt.strip():
        return cfg_prompt.strip()
    if isinstance(getattr(agent, 'description', None), str) and agent.description.strip():
        return agent.description.strip()
    return ''


def _safe_last_route_info(router_obj: ChatRouter) -> dict[str, Any]:
    """目的：安全取得模型路由資訊。
    為什麼：不同 LLM backend 不保證存在同名方法，需保留相容性。
    """
    try:
        getter = getattr(router_obj._llm, 'last_route_info', None)
        if callable(getter):
            info = getter()
            if isinstance(info, dict):
                return info
    except Exception:
        pass
    return {}


def _estimate_token_count(text: str) -> int:
    """目的：估算 token 數。
    為什麼：部分供應商未回傳 usage，仍需提供 llm_turns 的基礎審計資訊。
    """
    if not isinstance(text, str) or not text:
        return 0
    return max(1, len(text) // 4)


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _to_float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _normalize_usage_payload(*, usage_raw: Any, user_text: str, assistant_text: str) -> dict[str, Any]:
    """目的：統一 usage 結構。
    為什麼：不同供應商欄位命名不同，需落地一致格式供查詢與報表使用。
    """
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    input_tokens = (
        _to_int_or_none(usage.get('input'))
        or _to_int_or_none(usage.get('prompt_tokens'))
        or _to_int_or_none(usage.get('input_tokens'))
        or _estimate_token_count(user_text)
    )
    output_tokens = (
        _to_int_or_none(usage.get('output'))
        or _to_int_or_none(usage.get('completion_tokens'))
        or _to_int_or_none(usage.get('output_tokens'))
        or _estimate_token_count(assistant_text)
    )
    total_tokens = (
        _to_int_or_none(usage.get('total_tokens'))
        or _to_int_or_none(usage.get('total'))
        or int((input_tokens or 0) + (output_tokens or 0))
    )
    return {
        'input_tokens': int(input_tokens or 0),
        'output_tokens': int(output_tokens or 0),
        'total_tokens': int(total_tokens or 0),
        'raw': usage,
    }


def _estimate_cost_usd(*, provider: str, model: str, usage: dict[str, Any], route_info: dict[str, Any]) -> Decimal | None:
    """目的：估算每輪成本（USD）。
    為什麼：成本欄位是 llm_turns 核心審計資料，需在供應商未回傳時計算近似值。
    """
    for key in ('cost_usd', 'cost'):
        direct = _to_float_or_none(route_info.get(key))
        if direct is None and isinstance(usage.get('raw'), dict):
            direct = _to_float_or_none((usage.get('raw') or {}).get(key))
        if direct is not None and direct >= 0:
            return Decimal(f"{direct:.6f}")

    in_rate, out_rate = _resolve_price_rates(provider=provider, model=model)
    if in_rate is None or out_rate is None:
        return None

    input_tokens = int(usage.get('input_tokens') or 0)
    output_tokens = int(usage.get('output_tokens') or 0)
    estimated = (input_tokens / 1000.0) * in_rate + (output_tokens / 1000.0) * out_rate
    return Decimal(f"{estimated:.6f}")


@lru_cache(maxsize=1)
def _load_cost_table_from_settings() -> dict[tuple[str, str], tuple[float, float]]:
    """目的：讀取環境中的成本估算表。
    為什麼：將價格配置化，避免硬編碼散落在聊天流程，便於運維調整。
    """
    raw = str(getattr(settings, 'LLM_COST_TABLE_JSON', '') or '').strip()
    if not raw:
        return {}
    try:
        parsed = _json.loads(raw)
    except Exception as e:
        _log.warning('llm.cost_table.invalid_json', error=str(e))
        return {}
    if not isinstance(parsed, dict):
        _log.warning('llm.cost_table.invalid_schema', reason='top_level_not_object')
        return {}

    output: dict[tuple[str, str], tuple[float, float]] = {}
    invalid_items = 0
    for key, value in parsed.items():
        if not isinstance(key, str) or ':' not in key or not isinstance(value, dict):
            invalid_items += 1
            continue
        provider, model = key.split(':', 1)
        input_per_1k = _to_float_or_none(value.get('input_per_1k'))
        output_per_1k = _to_float_or_none(value.get('output_per_1k'))
        if input_per_1k is None or output_per_1k is None:
            invalid_items += 1
            continue
        if input_per_1k < 0 or output_per_1k < 0:
            invalid_items += 1
            continue
        output[(provider.strip().lower(), model.strip().lower())] = (input_per_1k, output_per_1k)

    if invalid_items > 0:
        _log.warning('llm.cost_table.invalid_items', invalid_items=invalid_items, valid_items=len(output))
    if not output:
        _log.warning('llm.cost_table.empty_after_parse')
    return output


def _default_cost_table() -> dict[tuple[str, str], tuple[float, float]]:
    """目的：提供內建成本估算表。
    為什麼：在未配置環境變數時，仍能提供可用的審計近似值。
    """
    return {
        ('openai', 'gpt-4o'): (0.005, 0.015),
        ('openai', 'gpt-4o-mini'): (0.00015, 0.0006),
        ('anthropic', 'claude-3-5-sonnet'): (0.003, 0.015),
        ('gemini', 'gemini-1.5-pro'): (0.0035, 0.0105),
    }


def _resolve_price_rates(*, provider: str, model: str) -> tuple[float | None, float | None]:
    """目的：解析 provider/model 對應的 input/output 單價。
    為什麼：支援設定覆寫與內建預設雙路徑，降低維護風險。
    """
    provider_key = str(provider or '').lower()
    model_key = str(model or '').lower()
    custom = _load_cost_table_from_settings()
    table = custom if custom else _default_cost_table()
    for (pv, mk), (in_price, out_price) in table.items():
        if pv == provider_key and mk in model_key:
            return in_price, out_price
    return None, None


def _create_llm_turn_record(
    *,
    db: Session,
    conversation_id: str,
    agent_id: str,
    user_message_id: str | None,
    assistant_message_id: str | None,
    provider: str | None,
    model: str | None,
    tier: str | None,
    system_prompt_snapshot: str,
    context_snapshot: dict[str, Any],
    usage: dict[str, Any],
    cost_usd: Decimal | None,
    latency_ms: int | None,
    status: str,
    error: str | None,
) -> None:
    """目的：寫入 llm_turns 審計資料。
    為什麼：聊天流程不應因審計寫入失敗而中斷，需集中保護。
    """
    try:
        row = LlmTurn(
            conversation_id=conversation_id,
            agent_id=agent_id,
            message_user_id=user_message_id,
            message_assistant_id=assistant_message_id,
            provider=provider,
            model=model,
            tier=tier,
            system_prompt_snapshot=system_prompt_snapshot,
            context_snapshot=context_snapshot,
            usage=usage,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            error=error,
        )
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()


def _get_by_path(obj: dict, path: str):
    """目的：讀取巢狀欄位（a.b.c）。
    為什麼：WS 與 stdio 兩條串流共用同一欄位解析邏輯。
    """
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


def _pick_worker_agent(*, db: Session, router_agent: Agent, message: str) -> Agent | None:
    """目的：以輕量規則選擇工作代理者。
    為什麼：在不引入額外編排框架前，先提供可用的主從分派能力。
    """
    workers = db.query(Agent).filter(Agent.id != router_agent.id).all()
    if not workers:
        return None
    lower_msg = str(message or '').lower()
    # 優先依名稱/描述關鍵字匹配
    for w in workers:
        name = str(w.name or '').strip().lower()
        desc = str(w.description or '').strip().lower()
        if (name and name in lower_msg) or (desc and any(tok and tok in lower_msg for tok in desc.split()[:5])):
            return w
    return workers[0]


def _write_event_part_safe(*, db: Session, conversation_id: str, type_: str, payload: dict[str, Any]) -> None:
    """目的：將事件寫入 event_parts，失敗時不中斷聊天流程。
    為什麼：路由事件屬於可觀測性資料，不應影響主要回覆可用性。
    """
    try:
        event_row = EventPart(conversation_id=conversation_id, type=type_, payload=payload)
        db.add(event_row)
        db.commit()
    except Exception:
        db.rollback()


def _parse_tool_call_block(buffer: str) -> tuple[bool, str, dict[str, Any]]:
    """目的：解析 [[CALL tool=...]] 區塊。
    為什麼：避免在串流主迴圈內混入字串解析細節，讓流程更聚焦。
    """
    marker = '[[CALL tool='
    pos = buffer.find(marker)
    if pos < 0:
        return False, '', {}

    tail = buffer[pos + len(marker):]
    end = tail.find(']]')
    if end < 0:
        return False, '', {}

    tool_name = tail[:end].strip()
    after = tail[end + 2:].lstrip()
    json_start = after.find('{')
    json_end = after.rfind('}')
    payload: dict[str, Any] = {}
    if json_start >= 0 and json_end >= 0 and json_end > json_start:
        try:
            payload = _json.loads(after[json_start:json_end + 1])
        except Exception:
            payload = {}
    return True, tool_name, payload


def _extract_progress_value(frame: dict[str, Any]) -> float | None:
    """目的：從工具 frame 取進度值。
    為什麼：stdio/ws 事件欄位命名可能不同，集中回退邏輯避免重複。
    """
    val = None
    if isinstance(frame.get('progress'), (int, float)):
        val = frame.get('progress')
    elif isinstance(frame.get('percent'), (int, float)):
        val = frame.get('percent')
    elif isinstance(frame.get('value'), (int, float)):
        val = frame.get('value')
    return float(val) if isinstance(val, (int, float)) else None


def _extract_eta_seconds(frame: dict[str, Any]) -> float | None:
    """目的：從工具 frame 取 ETA 秒數。
    為什麼：不同工具可能回傳 eta/eta_seconds/remaining_ms，需統一格式給前端。
    """
    if isinstance(frame.get('eta_seconds'), (int, float)):
        return float(frame.get('eta_seconds'))
    if isinstance(frame.get('eta'), (int, float)):
        return float(frame.get('eta'))
    if isinstance(frame.get('remaining_ms'), (int, float)):
        return float(frame.get('remaining_ms')) / 1000.0
    return None


@router.post('/conversations/{conversation_id}/tools/{tool_name}')
async def invoke_tool(
    conversation_id: str,
    tool_name: str,
    payload: dict = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Conversation', conversation_id)

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
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Conversation', conversation_id)

    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if conv is None:
        raise not_found_error('Conversation', conversation_id)

    agent = db.query(Agent).filter(Agent.id == conv.agent_id).first()
    if agent is None:
        raise not_found_error('Agent', str(conv.agent_id))

    router = ChatRouter()

    async def _gen():
        # 準備白名單/連線映射，並加入心跳機制
        router._prepare_integrations(db=db, agent_id=str(agent.id))
        import asyncio, time
        HEARTBEAT_SEC = 10.0
        last_emit = time.monotonic()
        queue: 'asyncio.Queue[str]' = asyncio.Queue()
        running = True

        async def _hb_loop():
            nonlocal last_emit, running
            try:
                while running:
                    await asyncio.sleep(HEARTBEAT_SEC)
                    if (time.monotonic() - last_emit) >= (HEARTBEAT_SEC - 0.5):
                        await queue.put(f"data: {_json.dumps({'type':'heartbeat','ts': time.time()}, ensure_ascii=False)}\n\n")
            except asyncio.CancelledError:
                pass

        hb_task = asyncio.create_task(_hb_loop())

        async def _emit(payload: dict | str):
            nonlocal last_emit
            last_emit = time.monotonic()
            if isinstance(payload, str):
                await queue.put(payload)
            else:
                await queue.put(f"data: {_json.dumps(payload, ensure_ascii=False)}\n\n")

        try:
            # 僅對 mcp:* 嘗試串流：優先 stdio 其次 WS；否則回退單段
            if tool_name.startswith('mcp:') and hasattr(router, '_mcp'):
                conn_name = tool_name.split(':', 1)[1]
                conn = getattr(router, '_mcp_map', {}).get(conn_name)
                # 1) stdio 模式：使用持久 stdio 串流
                if conn and str(conn.get('transport') or '').strip() == 'stdio':
                    cmd = str(conn.get('command') or '').strip()
                    args = conn.get('args') if isinstance(conn.get('args'), list) else []
                    env = conn.get('env') if isinstance(conn.get('env'), dict) else {}
                    # Why: progress/eta 欄位支援可配置路徑，避免綁定單一 MCP 回傳格式。
                    progress_key = (conn.get('progress_field') or '').strip() if isinstance(conn, dict) else ''
                    eta_key = (conn.get('eta_field') or '').strip() if isinstance(conn, dict) else ''
                    last_prog_ts = 0.0
                    last_prog_val: float | None = None
                    async for frame in router._mcp.stream_rpc_call_stdio(
                        command=cmd, args=args, env=env,
                        method='tools.invoke', params={'tool': conn_name, 'arguments': {}},
                    ):
                        try:
                            if isinstance(frame, dict):
                                val = _get_by_path(frame, progress_key) if progress_key else None
                                if not isinstance(val, (int, float)):
                                    if isinstance(frame.get('progress'), (int, float)):
                                        val = frame.get('progress')
                                    elif isinstance(frame.get('percent'), (int, float)):
                                        val = frame.get('percent')
                                    elif isinstance(frame.get('value'), (int, float)):
                                        val = frame.get('value')
                                if val is not None:
                                    now = time.monotonic()
                                    fval = float(val)
                                    should_emit = (now - last_prog_ts >= 0.2) or (last_prog_val is None) or (abs(fval - last_prog_val) >= 1.0)
                                    if should_emit:
                                        last_prog_ts = now
                                        last_prog_val = fval
                                        prog_payload = { 'type': 'progress', 'name': tool_name, 'value': fval }
                                        eta = _get_by_path(frame, eta_key) if eta_key else None
                                        if isinstance(eta, (int, float)):
                                            prog_payload['eta_seconds'] = float(eta)
                                        await _emit(prog_payload)
                        except Exception:
                            pass
                        await _emit({'type': 'tool', 'name': tool_name, 'frame': frame})
                    await _emit({'type': 'done'})
                    running = False
                    while not queue.empty():
                        yield await queue.get()
                    return
                if conn and conn.get('base_url'):
                    base_url = str(conn.get('base_url') or '').strip()
                    auth = conn.get('auth') if isinstance(conn, dict) else None
                    # Why: stdio 與 WS 路徑共用同一組 progress/eta 解析規則，保持前端事件一致。
                    progress_key = (conn.get('progress_field') or '').strip() if isinstance(conn, dict) else ''
                    eta_key = (conn.get('eta_field') or '').strip() if isinstance(conn, dict) else ''
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
                                val = _get_by_path(frame, progress_key) if progress_key else None
                                if not isinstance(val, (int, float)):
                                    if isinstance(frame.get('progress'), (int, float)):
                                        val = frame.get('progress')
                                    elif isinstance(frame.get('percent'), (int, float)):
                                        val = frame.get('percent')
                                    elif isinstance(frame.get('value'), (int, float)):
                                        val = frame.get('value')
                                if val is not None:
                                    eta = _get_by_path(frame, eta_key) if eta_key else None
                                    if not isinstance(eta, (int, float)):
                                        if isinstance(frame.get('eta_seconds'), (int, float)):
                                            eta = frame.get('eta_seconds')
                                        elif isinstance(frame.get('eta'), (int, float)):
                                            eta = frame.get('eta')
                                        elif isinstance(frame.get('remaining_ms'), (int, float)):
                                            rem_ms = frame.get('remaining_ms')
                                            eta = (rem_ms / 1000.0) if isinstance(rem_ms, (int, float)) else None
                                    now = time.monotonic()
                                    fval = float(val)
                                    should_emit = (now - last_prog_ts >= 0.2) or (last_prog_val is None) or (abs(fval - last_prog_val) >= 1.0)
                                    if should_emit:
                                        last_prog_ts = now
                                        last_prog_val = fval
                                        prog_payload = { 'type': 'progress', 'name': tool_name, 'value': fval }
                                        if isinstance(eta, (int, float)):
                                            prog_payload['eta_seconds'] = float(eta)
                                        await _emit(prog_payload)
                        except Exception:
                            pass
                        await _emit({'type': 'tool', 'name': tool_name, 'frame': frame})
                    # 發送完成事件
                    await _emit({'type': 'done'})
                    running = False
                    # 將剩餘排隊事件送出
                    while not queue.empty():
                        yield await queue.get()
                    return
            # 回退：單段結果
            res = await router.call_tool_async(session_id=str(conv.id), tool=tool_name, payload={}, db=db, agent_id=str(agent.id))
            await _emit({'type': 'tool', 'name': tool_name, 'frame': res})
            await _emit({'type': 'done'})
            running = False
            while not queue.empty():
                yield await queue.get()
        finally:
            try:
                running = False
                hb_task.cancel()
            except Exception:
                pass

    return StreamingResponse(_gen(), media_type='text/event-stream')


@router.get('/agents/{agent_id}/chat/stream')
async def chat_stream(
    agent_id: str,  # 代理者的唯一标识符，用于识别具体的AI代理
    message: str,  # 用户输入的消息内容，需要AI代理处理和回应
    db: Session = Depends(get_db),  # 数据库会话依赖，用于数据库操作
    current_user: User = Depends(get_current_user),  # 当前用户依赖，获取当前登录用户信息
):
    """SSE 串流聊天：以 LLM 串流文字增量，遇到工具呼叫（[[CALL tool=...]]+JSON）即時串流工具結果。"""
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    if not message or len(message.strip()) == 0:
        raise validation_error('Message cannot be empty')

    # Why: 串流與同步路徑共用同一會話/寫盤相容策略，避免切頁後資料差異。
    conversation = _query_latest_conversation_with_fallback(db=db, agent_id=agent_id, user_id=current_user.id)
    if not conversation:
        conversation = _create_conversation_with_fallback(db=db, agent_id=agent_id, user_id=current_user.id)

    user_message = Message(conversation_id=conversation.id, role='user', content=message)
    _save_message_with_touch_fallback(db=db, conversation=conversation, message_obj=user_message)

    router = ChatRouter()
    overrides = _build_agent_overrides(agent)
    _apply_custom_toolcall_guide(router, agent)

    try:
        router._llm.init_for_session(session_id=str(conversation.id), preferred_tier=overrides.get('tier'), overrides=overrides)
    except TypeError:
        router._llm.init_for_session(session_id=str(conversation.id), preferred_tier=overrides.get('tier'))

    agent_ctx = router._prepare_integrations(db=db, agent_id=str(agent.id))
    composed_user_message = _build_capability_prompt(router_obj=router, agent_ctx=agent_ctx, message=message)
    started_at = time.monotonic()
    system_prompt_snapshot = _resolve_agent_system_prompt_snapshot(agent)
    turn_status = 'success'
    turn_error: str | None = None

    # 無可用模型路由時，依設定採硬失敗
    if getattr(settings, 'LLM_HARD_FAIL_ON_NO_ROUTE', False):
        hc = router._llm.health_check(mode='soft')
        if not bool((hc or {}).get('ok')):
            reason = (hc or {}).get('error') or '模型路由不可用'
            route_info = _safe_last_route_info(router)
            context_snapshot = {
                'skills': list(agent_ctx.get('skills') or []),
                'mcp_names': [str(c.get('name')) for c in (agent_ctx.get('mcp') or []) if isinstance(c, dict) and c.get('name')],
                'rag': agent_ctx.get('rag') or {},
                'route': route_info,
                'entry': 'agents.chat.stream',
            }
            usage = _normalize_usage_payload(usage_raw=route_info.get('usage'), user_text=message, assistant_text='')
            provider = str(route_info.get('provider') or overrides.get('provider') or '')
            model = str(route_info.get('model') or overrides.get('model') or '')
            _create_llm_turn_record(
                db=db,
                conversation_id=str(conversation.id),
                agent_id=str(agent.id),
                user_message_id=str(user_message.id),
                assistant_message_id=None,
                provider=provider,
                model=model,
                tier=str(route_info.get('tier') or overrides.get('tier') or ''),
                system_prompt_snapshot=system_prompt_snapshot,
                context_snapshot=context_snapshot,
                usage=usage,
                cost_usd=_estimate_cost_usd(provider=provider, model=model, usage=usage, route_info=route_info),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                status='no_route',
                error=str(reason),
            )

            async def _gen_unavailable():
                yield f"data: {_json.dumps({'type':'react','phase':'reroute','message':'策略改選：模型路由不可用，改為降級訊息','reason_code':'model.no_route','from':'llm','to':'unavailable_text','reason':reason}, ensure_ascii=False)}\n\n"
                text = f"模型路由暫時不可用（{reason}）。請稍後重試，或改用其他代理者模型。"
                yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(text, ensure_ascii=False)} }}\n\n"
                yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
            return StreamingResponse(_gen_unavailable(), media_type='text/event-stream')

    async def _gen():
        nonlocal turn_status, turn_error
        buffer = ''
        detected_tool = False
        tool_name = ''
        tool_payload: dict[str, Any] = {}
        react_step = 0
        max_steps = int(getattr(settings, 'REACT_MAX_STEPS', 3) or 3)

        def _react_event(phase: str, message: str, extra: dict[str, Any] | None = None) -> str:
            payload = {"type": "react", "phase": phase, "message": message}
            if extra:
                payload.update(extra)
            return f"data: {_json.dumps(payload, ensure_ascii=False)}\\n\\n"

        async def _fallback_general_answer(reason: str) -> str:
            prompt = (
                "你是一個客服助理。工具暫時不可用，請直接用一般知識回答，不要呼叫任何工具，"
                "也不要輸出 [[CALL ...]] 區塊。\n"
                f"工具錯誤：{reason}\n"
                f"使用者問題：{message}\n"
            )
            try:
                chunks: list[str] = []
                async for part in _stream_complete_async(router._llm, prompt=prompt, tier=overrides.get('tier')):
                    if isinstance(part, str) and part:
                        chunks.append(part)
                        if sum(len(x) for x in chunks) >= 600:
                            break
                text = ''.join(chunks)
                if isinstance(text, str) and text.strip():
                    cleaned = text.replace('[[CALL', '[CALL')
                    return cleaned
            except Exception:
                pass
            return "目前工具暫時不可用，我先用一般知識回覆：今天台北通常為多雲到晴，實際降雨與溫度請以氣象署最新公告為準。"

        async for delta in _stream_complete_async(router._llm, prompt=composed_user_message, tier=overrides.get('tier')):
            if not isinstance(delta, str):
                continue
            buffer += delta
            if not detected_tool:
                has_call, parsed_tool_name, parsed_payload = _parse_tool_call_block(buffer)
                if has_call:
                    tool_name = parsed_tool_name
                    tool_payload = parsed_payload
                    detected_tool = True
                    react_step += 1
                    if react_step > max_steps:
                        turn_status = 'error'
                        turn_error = 'react_step_limit'
                        yield _react_event("finish", f"ReAct 步驟超過上限（{max_steps}）", {"steps": react_step, "error": "react_step_limit"})
                        yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps('工具呼叫步驟過多，已停止本輪執行。請精簡問題後重試。', ensure_ascii=False)} }}\n\n"
                        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                        return
                    yield _react_event(
                        phase="plan",
                        message=f"規劃第 {react_step} 步，準備呼叫工具 {tool_name}",
                        extra={"step": react_step, "tool": tool_name, "args": tool_payload or {}},
                    )
            # 推送文字增量
            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(delta, ensure_ascii=False)} }}\n\n"

            if detected_tool:
                detected_tool = False
                yield _react_event(
                    phase="act_start",
                    message=f"開始執行工具 {tool_name}",
                    extra={"step": react_step, "tool": tool_name},
                )
                # 通知前端工具開始（附上參數以支援前端重執行）
                start_payload = {"type": "tool_start", "name": tool_name, "args": tool_payload or {}}
                yield f"data: {_json.dumps(start_payload, ensure_ascii=False)}\n\n"
                if tool_name.startswith('mcp:') and hasattr(router, '_mcp'):
                    conn = getattr(router, '_mcp_map', {}).get(tool_name.split(':',1)[1])
                    # stdio 模式：持久 stdio 串流
                    if conn and str(conn.get('transport') or '').strip() == 'stdio':
                        cmd = str(conn.get('command') or '').strip()
                        args = conn.get('args') if isinstance(conn.get('args'), list) else []
                        env = conn.get('env') if isinstance(conn.get('env'), dict) else {}
                        tool_timeout_s = float(getattr(settings, 'MCP_TOOL_TIMEOUT_SEC', 30) or 30)
                        try:
                            async with asyncio.timeout(tool_timeout_s):
                                async for frame in router._mcp.stream_rpc_call_stdio(
                                    command=cmd,
                                    args=args,
                                    env=env,
                                    method='tools.invoke',
                                    params={'tool': tool_name.split(':',1)[1], 'arguments': tool_payload or {}},
                                ):
                                    # 嘗試發出進度（若回傳內含進度欄位）
                                    try:
                                        if isinstance(frame, dict):
                                            val = _extract_progress_value(frame)
                                            if val is not None:
                                                prog_payload = { 'type': 'progress', 'name': tool_name, 'value': val }
                                                yield f"data: {_json.dumps(prog_payload, ensure_ascii=False)}\n\n"
                                    except Exception:
                                        pass
                                    yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(frame, ensure_ascii=False)} }}\n\n"
                                    ok = frame.get('ok') if isinstance(frame, dict) else None
                                    if ok is True:
                                        yield _react_event("act_result", f"工具 {tool_name} 執行成功", {"step": react_step, "tool": tool_name, "ok": True})
                                    elif ok is False:
                                        turn_status = 'tool_error'
                                        turn_error = str(frame.get('error') if isinstance(frame, dict) else 'tool_error')
                                        yield _react_event("act_result", f"工具 {tool_name} 執行失敗", {"step": react_step, "tool": tool_name, "ok": False, "error": frame.get('error') if isinstance(frame, dict) else None})
                                        yield _react_event("reroute", "策略改選：工具失敗，改為一般回覆模式", {"reason_code": "tool.error", "from": tool_name, "to": "llm_fallback", "error": frame.get('error') if isinstance(frame, dict) else None})
                                        fb = await _fallback_general_answer(str(frame.get('error') if isinstance(frame, dict) else 'tool_error'))
                                        yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
                                        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                                        return
                        except TimeoutError:
                            turn_status = 'timeout'
                            turn_error = f'mcp_tool_timeout_{int(tool_timeout_s)}s'
                            timeout_frame = {'ok': False, 'error': f'mcp_tool_timeout_{int(tool_timeout_s)}s'}
                            yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(timeout_frame, ensure_ascii=False)} }}\n\n"
                            yield _react_event("act_result", f"工具 {tool_name} 執行逾時", {"step": react_step, "tool": tool_name, "ok": False, "error": timeout_frame['error']})
                            yield _react_event("reroute", "策略改選：工具逾時，改為一般回覆模式", {"reason_code": "tool.timeout", "from": tool_name, "to": "llm_fallback", "error": timeout_frame['error']})
                            fb = await _fallback_general_answer(timeout_frame['error'])
                            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
                            yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                            return
                        except Exception:
                            res = await router.call_tool_async(session_id=str(conversation.id), tool=tool_name, payload=tool_payload or {}, db=db, agent_id=str(agent.id))
                            yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(res, ensure_ascii=False)} }}\n\n"
                            yield _react_event("act_result", f"工具 {tool_name} 已回傳結果", {"step": react_step, "tool": tool_name, "ok": bool((res or {}).get('ok', True))})
                    elif conn and conn.get('base_url'):
                        base_url = str(conn.get('base_url') or '').strip()
                        auth = conn.get('auth') if isinstance(conn, dict) else None
                        tool_timeout_s = float(getattr(settings, 'MCP_TOOL_TIMEOUT_SEC', 30) or 30)
                        try:
                            async with asyncio.timeout(tool_timeout_s):
                                async for frame in router._mcp.stream_rpc_call_ws(
                                    base_url=base_url,
                                    method='tools.invoke',
                                    params={'tool': tool_name.split(':',1)[1], 'arguments': tool_payload or {}},
                                    auth=auth if isinstance(auth, dict) else None,
                                ):
                                    # 標準化進度事件
                                    try:
                                        if isinstance(frame, dict):
                                            val = _extract_progress_value(frame)
                                            if val is not None:
                                                eta = _extract_eta_seconds(frame)
                                                prog_payload = { 'type': 'progress', 'name': tool_name, 'value': val }
                                                if eta is not None:
                                                    prog_payload['eta_seconds'] = eta
                                                yield f"data: {_json.dumps(prog_payload, ensure_ascii=False)}\n\n"
                                    except Exception:
                                        pass
                                    yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(frame, ensure_ascii=False)} }}\n\n"
                                    ok = frame.get('ok') if isinstance(frame, dict) else None
                                    if ok is True:
                                        yield _react_event("act_result", f"工具 {tool_name} 執行成功", {"step": react_step, "tool": tool_name, "ok": True})
                                    elif ok is False:
                                        turn_status = 'tool_error'
                                        turn_error = str(frame.get('error') if isinstance(frame, dict) else 'tool_error')
                                        yield _react_event("act_result", f"工具 {tool_name} 執行失敗", {"step": react_step, "tool": tool_name, "ok": False, "error": frame.get('error') if isinstance(frame, dict) else None})
                                        yield _react_event("reroute", "策略改選：工具失敗，改為一般回覆模式", {"reason_code": "tool.error", "from": tool_name, "to": "llm_fallback", "error": frame.get('error') if isinstance(frame, dict) else None})
                                        fb = await _fallback_general_answer(str(frame.get('error') if isinstance(frame, dict) else 'tool_error'))
                                        yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
                                        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                                        return
                        except TimeoutError:
                            turn_status = 'timeout'
                            turn_error = f'mcp_tool_timeout_{int(tool_timeout_s)}s'
                            timeout_frame = {'ok': False, 'error': f'mcp_tool_timeout_{int(tool_timeout_s)}s'}
                            yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(timeout_frame, ensure_ascii=False)} }}\n\n"
                            yield _react_event("act_result", f"工具 {tool_name} 執行逾時", {"step": react_step, "tool": tool_name, "ok": False, "error": timeout_frame['error']})
                            yield _react_event("reroute", "策略改選：工具逾時，改為一般回覆模式", {"reason_code": "tool.timeout", "from": tool_name, "to": "llm_fallback", "error": timeout_frame['error']})
                            fb = await _fallback_general_answer(timeout_frame['error'])
                            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
                            yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                            return
                        except Exception:
                            res = await router.call_tool_async(session_id=str(conversation.id), tool=tool_name, payload=tool_payload or {}, db=db, agent_id=str(agent.id))
                            yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(res, ensure_ascii=False)} }}\n\n"
                            yield _react_event("act_result", f"工具 {tool_name} 已回傳結果", {"step": react_step, "tool": tool_name, "ok": bool((res or {}).get('ok', True))})
                else:
                    res = await router.call_tool_async(session_id=str(conversation.id), tool=tool_name, payload=tool_payload or {}, db=db, agent_id=str(agent.id))
                    yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(res, ensure_ascii=False)} }}\n\n"
                    yield _react_event("act_result", f"工具 {tool_name} 已回傳結果", {"step": react_step, "tool": tool_name, "ok": bool((res or {}).get('ok', True))})
                    if not bool((res or {}).get('ok', True)):
                        turn_status = 'tool_error'
                        turn_error = str((res or {}).get('error') or 'tool_error')

        yield _react_event("finish", "本輪 ReAct 執行完成", {"steps": react_step})
        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"

    # 加入帶心跳版本的包裝，以避免長時間無資料時被中間層斷線
    async def _gen_hb():
        import asyncio, time
        HEARTBEAT_SEC = 10.0
        STREAM_TIMEOUT_SEC = float(getattr(settings, 'CHAT_STREAM_TIMEOUT_SEC', 120) or 120)
        last_emit = time.monotonic()
        started_at = time.monotonic()
        queue: 'asyncio.Queue[str]' = asyncio.Queue()
        running = True
        assistant_chunks: list[str] = []

        def _sanitize_text(text: str) -> str:
            out = str(text or '')
            out = re.sub(r"\[\[CALL tool=[^\]]+\]\]\s*(\{[\s\S]*?\})?", "", out)
            out = re.sub(r"```json\s*\{[\s\S]*?\}\s*```", "", out, flags=re.IGNORECASE)
            out = re.sub(r"\{\s*\"tool_calls\"\s*:\s*\[[\s\S]*?\]\s*\}", "", out)
            return out.strip()

        async def _hb_loop():
            nonlocal last_emit, running
            try:
                while running:
                    await asyncio.sleep(HEARTBEAT_SEC)
                    if (time.monotonic() - last_emit) >= (HEARTBEAT_SEC - 0.5):
                        await queue.put(f"data: {_json.dumps({'type':'heartbeat','ts': time.time()}, ensure_ascii=False)}\n\n")
            except asyncio.CancelledError:
                pass

        hb_task = asyncio.create_task(_hb_loop())

        async def _push_chunks():
            nonlocal last_emit, turn_status, turn_error
            try:
                async for chunk in _gen():
                    last_emit = time.monotonic()
                    try:
                        if isinstance(chunk, str) and chunk.startswith('data: '):
                            payload = _json.loads(chunk.replace('data: ', '', 1).strip())
                            if isinstance(payload, dict) and payload.get('type') == 'text':
                                d = payload.get('delta')
                                if isinstance(d, str) and d:
                                    assistant_chunks.append(d)
                    except Exception:
                        pass
                    await queue.put(chunk)
            except Exception as e:
                turn_status = 'error'
                turn_error = str(e)
                err_payload = {"type": "error", "message": str(e)}
                await queue.put(f"data: {_json.dumps(err_payload, ensure_ascii=False)}\n\n")
            finally:
                # 完成後結束
                await queue.put('__DONE__')

        prod_task = asyncio.create_task(_push_chunks())
        try:
            while True:
                if (time.monotonic() - started_at) > STREAM_TIMEOUT_SEC:
                    if turn_status == 'success':
                        turn_status = 'timeout'
                        turn_error = 'chat_stream_timeout'
                    busy = '系統忙碌中，請稍後再試。'
                    yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(busy, ensure_ascii=False)} }}\n\n"
                    yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if item == '__DONE__':
                    break
                yield item
        finally:
            try:
                running = False
                hb_task.cancel()
            except Exception:
                pass
            try:
                prod_task.cancel()
            except Exception:
                pass
            try:
                merged = _sanitize_text(''.join(assistant_chunks))
                assistant_message_id: str | None = None
                if merged:
                    assistant_message = Message(conversation_id=conversation.id, role='assistant', content=merged, timestamp=datetime.utcnow())
                    db.add(assistant_message)
                    conversation.last_interacted_at = datetime.utcnow()
                    db.commit()
                    db.refresh(assistant_message)
                    assistant_message_id = str(assistant_message.id)
            except Exception:
                db.rollback()
                assistant_message_id = None
            try:
                route_info = _safe_last_route_info(router)
                provider = str(route_info.get('provider') or overrides.get('provider') or '')
                model = str(route_info.get('model') or overrides.get('model') or '')
                tier_value = str(route_info.get('tier') or overrides.get('tier') or '')
                usage = _normalize_usage_payload(usage_raw=route_info.get('usage'), user_text=message, assistant_text=merged)
                context_snapshot = {
                    'skills': list(agent_ctx.get('skills') or []),
                    'mcp_names': [str(c.get('name')) for c in (agent_ctx.get('mcp') or []) if isinstance(c, dict) and c.get('name')],
                    'rag': agent_ctx.get('rag') or {},
                    'route': route_info,
                    'entry': 'agents.chat.stream',
                }
                _create_llm_turn_record(
                    db=db,
                    conversation_id=str(conversation.id),
                    agent_id=str(agent.id),
                    user_message_id=str(user_message.id),
                    assistant_message_id=assistant_message_id,
                    provider=provider,
                    model=model,
                    tier=tier_value,
                    system_prompt_snapshot=system_prompt_snapshot,
                    context_snapshot=context_snapshot,
                    usage=usage,
                    cost_usd=_estimate_cost_usd(provider=provider, model=model, usage=usage, route_info=route_info),
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    status=(turn_status if (turn_status != 'success' or assistant_message_id) else 'error'),
                    error=(turn_error if turn_error else (None if assistant_message_id else 'assistant_message_not_persisted')),
                )
            except Exception:
                pass

    return StreamingResponse(_gen_hb(), media_type='text/event-stream')


@router.post('/chat')
async def chat_entry_router(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """統一聊天入口：先經主代理分派，再轉發到目標代理者串流回覆。"""
    _require_chat_permission(current_user)
    message = str((payload or {}).get('message') or '').strip()
    if not message:
        raise validation_error('Message cannot be empty')

    router_agent = db.query(Agent).filter(Agent.is_router == True).first()  # noqa: E712
    if router_agent is None:
        router_agent = db.query(Agent).first()
    if router_agent is None:
        raise validation_error('尚未建立可用代理者')

    worker = _pick_worker_agent(db=db, router_agent=router_agent, message=message)
    is_fallback = False
    if worker is None:
        worker = router_agent
        is_fallback = True

    conversation = _query_latest_conversation_with_fallback(db=db, agent_id=str(worker.id), user_id=current_user.id)
    if conversation is None:
        conversation = _create_conversation_with_fallback(db=db, agent_id=str(worker.id), user_id=current_user.id)

    route_decision_payload = {
        'router_agent_id': str(router_agent.id),
        'target_agent_id': str(worker.id),
        'target_agent_name': worker.name,
        'reason': 'worker_not_found_fallback' if is_fallback else 'keyword_or_default',
    }
    _write_event_part_safe(
        db=db,
        conversation_id=str(conversation.id),
        type_='route.decision',
        payload=route_decision_payload,
    )
    _write_event_part_safe(
        db=db,
        conversation_id=str(conversation.id),
        type_='route.forward',
        payload={'from_agent_id': str(router_agent.id), 'to_agent_id': str(worker.id)},
    )
    if is_fallback:
        _write_event_part_safe(
            db=db,
            conversation_id=str(conversation.id),
            type_='route.fallback',
            payload={'reason': 'worker_not_found', 'target_agent_id': str(worker.id)},
        )

    routed_response = await chat_stream(
        agent_id=str(worker.id),
        message=message,
        db=db,
        current_user=current_user,
    )

    async def _with_route_event():
        event = {
            'type': 'route.decision',
            **route_decision_payload,
            'conversation_id': str(conversation.id),
        }
        yield f"data: {_json.dumps(event, ensure_ascii=False)}\n\n"
        async for chunk in routed_response.body_iterator:
            yield chunk

    return StreamingResponse(_with_route_event(), media_type='text/event-stream')


@router.get('/conversations/{conversation_id}/events')
async def list_conversation_events(
    conversation_id: str,
    types: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Conversation', conversation_id)

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


@router.get('/conversations/{conversation_id}/routing-events')
async def list_conversation_routing_events(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Conversation', conversation_id)

    rows = db.query(EventPart).filter(
        EventPart.conversation_id == conversation_id,
        EventPart.type.in_(['route.decision', 'route.forward', 'route.fallback']),
    ).order_by(EventPart.created_at.asc()).all()

    return {
        'events': [
            {
                'id': str(row.id),
                'type': row.type,
                'payload': row.payload,
                'created_at': row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@router.get('/agents/{agent_id}/conversations')
async def get_conversations(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # Why: 與聊天端點共用同樣 schema 相容策略，避免列表與對話結果不一致。
    latest = _query_latest_conversation_with_fallback(db=db, agent_id=agent_id, user_id=current_user.id)
    if latest is None:
        conversations = []
    else:
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
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    conv = _create_conversation_with_fallback(db=db, agent_id=agent_id, user_id=current_user.id)

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
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Conversation', conversation_id)

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


@router.delete('/conversations/{conversation_id}')
async def delete_conversation(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """刪除一個會話（僅限目前使用者擁有的會話）。"""
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Conversation', conversation_id)

    try:
        conv = db.query(Conversation).filter(
            Conversation.id == conversation_id,
            (Conversation.user_id == current_user.id),
        ).first()
    except (ProgrammingError, OperationalError) as e:
        _log.warning("db.migration.missing_columns", hint="conversations.user_id", error=str(e))
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()

    if not conv:
        raise not_found_error('Conversation', conversation_id)

    try:
        db.query(EventPart).filter(EventPart.conversation_id == conv.id).delete(synchronize_session=False)
        db.query(Message).filter(Message.conversation_id == conv.id).delete(synchronize_session=False)
        db.delete(conv)
        db.commit()
    except Exception:
        db.rollback()
        raise validation_error('刪除會話失敗，請稍後再試')

    return {'ok': True, 'conversation_id': str(conversation_id)}
