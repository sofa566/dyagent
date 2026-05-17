from fastapi import APIRouter, Depends, Body, File, Form, UploadFile
from typing import Any, AsyncGenerator
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
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from src.core.database import get_db
from src.models import User, Agent, Conversation, Message, LlmTurn, MultiAgentSession, MultiAgentTask, SkillEntry, SkillInteraction
from src.models.events import EventPart
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error, forbidden_error
from src.services.chat_router import ChatRouter
from src.services.embedding_service import embedding_service
from src.services.llm_client import LLMClient
from src.services.chat_attachment_service import chat_attachment_service
from src.services.skill_registry import get_skill_registry, get_skill_rule_router
from src.core.logging import get_logger
from src.core.config import settings

from src.api.routes.chat_tools import _classify_routing, _cosine_similarity

router = APIRouter()
_log = get_logger("api.chat")

REFERENCE_FETCH_TIMEOUT_SECONDS = 8
REFERENCE_FETCH_MAX_CHARS = 6000
REFERENCE_FETCH_MAX_URLS = 2


def _strip_system_reminder_text(text: str) -> str:
    """目的：移除不應出現在使用者回覆中的 system reminder 文本。
    為什麼：避免模型誤回顯執行環境提醒，污染最終對話內容。
    """
    raw = str(text or '')
    if not raw:
        return ''

    cleaned = re.sub(r'<system-reminder>[\s\S]*?</system-reminder>', '', raw, flags=re.IGNORECASE)
    cleaned = re.sub(r'</?system-reminder>', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r'Your operational mode has changed from plan to build\.[\s\S]*?tools as needed\.',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r'^\s*Your operational mode has changed from plan to build\.\s*$', '', cleaned, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = re.sub(r'^\s*You are no longer in read-only mode\.\s*$', '', cleaned, flags=re.IGNORECASE | re.MULTILINE)
    cleaned = re.sub(
        r'^\s*You are permitted to make file changes, run shell commands, and utilize your arsenal of tools as needed\.\s*$',
        '',
        cleaned,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return cleaned.strip()


def _strip_tool_protocol_text(text: str) -> str:
    """目的：移除工具呼叫協定殘留，避免回覆內容被內部協定污染。
    為什麼：模型可能輸出完整或不完整的 tool_calls JSON，需要統一清理策略。
    """
    out = str(text or '')
    if not out:
        return ''
    out = re.sub(r"\[\[CALL tool=[^\]]+\]\]\s*(\{[\s\S]*?\})?", "", out)
    out = re.sub(r"```json\s*\{[\s\S]*?\}\s*```", "", out, flags=re.IGNORECASE)
    out = re.sub(r"```json[\s\S]*?\"tool_calls\"[\s\S]*?```", "", out, flags=re.IGNORECASE)
    out = re.sub(r"\{\s*\"tool_calls\"\s*:\s*\[[\s\S]*?\]\s*\}", "", out)
    out = re.sub(r"\{\s*\"tool_calls\"\s*:\s*\[[\s\S]*?\]\s*", "", out)
    out = re.sub(r"\{\s*\"tool_calls\"\s*:\s*\[[^\n\r]*", "", out)
    out = _strip_system_reminder_text(out)
    return out.strip()


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
            _log.debug("==> LLM stream producer finished")
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
        setattr(conversation, 'last_interacted_at', datetime.now())
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


def _build_capability_prompt(*, router_obj: ChatRouter, agent_ctx: dict[str, Any], message: str, history_context: str = '') -> str:
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
    history_text = str(history_context or '').strip()
    if history_text:
        return f"{prefix}{guide}\n{history_text}\n\n[Current User Message]\n{message}"
    return f"{prefix}{guide}\n{message}"


def _build_recent_history_context(
    *,
    db: Session,
    conversation_id: Any,
    max_messages: int,
    max_tokens: int,
    include_tool_text: bool,
    exclude_message_id: Any | None = None,
) -> str:
    """目的：組裝同一會話最近歷史，供本輪 prompt 延續上下文。
    為什麼：目前模型呼叫採單輪輸入，需顯式注入歷史才能維持前後文連貫。
    """
    if max_messages <= 0 or max_tokens <= 0:
        return ''

    try:
        scan_limit = max(max_messages * 4, max_messages + 4)
        rows = db.query(Message).filter(
            Message.conversation_id == conversation_id,
        ).order_by(Message.timestamp.desc()).limit(scan_limit).all()
    except Exception:
        return ''

    picked: list[tuple[str, str]] = []
    used_tokens = 0
    excluded_id = str(exclude_message_id) if exclude_message_id is not None else ''

    for row in rows:
        role = str(getattr(row, 'role', '') or '').strip().lower()
        if role not in {'user', 'assistant'}:
            continue
        row_id = str(getattr(row, 'id', '') or '')
        if excluded_id and row_id == excluded_id:
            continue

        raw_content = str(getattr(row, 'content', '') or '').strip()
        if not raw_content:
            continue
        content = raw_content if include_tool_text else _strip_tool_protocol_text(raw_content)
        content = content.strip()
        if not content:
            continue

        entry_tokens = _estimate_token_count(content) + 8
        if (used_tokens + entry_tokens) > max_tokens:
            break

        used_tokens += entry_tokens
        picked.append((role, content[:1200]))
        if len(picked) >= max_messages:
            break

    if not picked:
        return ''

    picked.reverse()
    lines = ['[Conversation History]', '以下為同一會話最近對話，請延續上下文回答：']
    for role, content in picked:
        label = '使用者' if role == 'user' else '助理'
        lines.append(f"- {label}: {content}")
    return '\n'.join(lines)


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


def _to_json_safe(value: Any) -> Any:
    """目的：將任意物件轉為可 JSON 序列化格式。
    為什麼：llm_turns 的 JSON 欄位若含不可序列化型別，會造成整筆審計寫入失敗。
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    try:
        _json.dumps(value, ensure_ascii=False)
        return value
    except Exception:
        return str(value)


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
        safe_context_snapshot = _to_json_safe(context_snapshot)
        safe_usage = _to_json_safe(usage)
        row = LlmTurn(
            conversation_id=conversation_id,
            agent_id=agent_id,
            message_user_id=user_message_id,
            message_assistant_id=assistant_message_id,
            provider=provider,
            model=model,
            tier=tier,
            system_prompt_snapshot=system_prompt_snapshot,
            context_snapshot=safe_context_snapshot,
            usage=safe_usage,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            error=error,
        )
        db.add(row)
        db.commit()
    except Exception as e:
        db.rollback()
        _log.error(
            'llm_turn.persist_failed',
            error=str(e),
            agent_id=str(agent_id),
            conversation_id=str(conversation_id),
            status=str(status),
        )


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



def _pick_worker_by_rules(*, workers: list[Agent], message: str) -> Agent | None:
    """目的：用名稱與描述關鍵詞做快速規則匹配。
    為什麼：在向量/LLM 路由前先走低成本判斷，但需避免過短 token 造成誤判。
    """
    lower_msg = str(message or '').lower()
    for worker in workers:
        name = str(worker.name or '').strip().lower()
        desc = str(worker.description or '').strip().lower()
        if name and name in lower_msg:
            return worker
        desc_tokens = [token for token in desc.split()[:8] if token and len(token) >= 2 and not token.isdigit()]
        if desc_tokens and any(token in lower_msg for token in desc_tokens):
            return worker
    return None


def _normalize_reference_url(raw_url: str) -> str:
    """目的：將常見參考網址正規化為可讀取內容端點。
    為什麼：Google Docs edit 連結通常無法直接抓正文，需轉成 export 端點。
    """
    candidate = str(raw_url or '').strip()
    if not candidate:
        return ''
    try:
        parsed = urlparse(candidate)
    except Exception:
        return ''
    if parsed.scheme not in {'http', 'https'}:
        return ''
    host = (parsed.netloc or '').lower()
    path = parsed.path or ''
    if 'docs.google.com' in host and '/document/d/' in path:
        parts = path.split('/document/d/', 1)
        tail = parts[1] if len(parts) > 1 else ''
        doc_id = tail.split('/', 1)[0].strip()
        if doc_id:
            return f'https://docs.google.com/document/d/{doc_id}/export?format=txt'
    return candidate


def _extract_reference_urls(message: str) -> list[str]:
    """目的：從訊息中抽取參考網址（含附加輸入區塊）。
    為什麼：在工具不可用時，後端可先抓取網頁文字，讓技能仍可處理內容。
    """
    text = str(message or '')
    if not text:
        return []
    url_pattern = re.compile(r'https?://[^\s)]+', re.IGNORECASE)
    urls = []
    for match in url_pattern.findall(text):
        normalized = _normalize_reference_url(match)
        if normalized and normalized not in urls:
            urls.append(normalized)
        if len(urls) >= REFERENCE_FETCH_MAX_URLS:
            break
    return urls


def _strip_html_tags(html: str) -> str:
    text = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.IGNORECASE)
    text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _fetch_reference_text(url: str) -> str:
    """目的：抓取單一參考網址文字內容。
    為什麼：補足未配置 mcp:fetch 的代理者，仍可使用外部參考內容回答。
    """
    req = Request(url, headers={'User-Agent': 'dyagent/1.0'})
    with urlopen(req, timeout=REFERENCE_FETCH_TIMEOUT_SECONDS) as response:  # nosec B310
        content_type = str(response.headers.get('Content-Type') or '').lower()
        raw = response.read(REFERENCE_FETCH_MAX_CHARS * 2)
    text = raw.decode('utf-8', errors='ignore')
    if 'text/html' in content_type:
        text = _strip_html_tags(text)
    text = text.strip()
    return text[:REFERENCE_FETCH_MAX_CHARS]


def _inject_reference_content(message: str) -> str:
    """目的：將可取得的參考網頁內容注入到訊息中。
    為什麼：當模型需改寫文章時，若只有網址無正文會降低可用性。
    """
    urls = _extract_reference_urls(message)
    if not urls:
        return message
    blocks = []
    for url in urls:
        try:
            fetched = _fetch_reference_text(url)
        except Exception as error:
            blocks.append(f'[參考內容抓取失敗]\n- url: {url}\n- error: {str(error)[:180]}')
            continue
        if fetched:
            blocks.append(f'[參考內容]\n- url: {url}\n{fetched}')
    if not blocks:
        return message
    return f"{message}\n\n[系統預抓參考內容]\n" + "\n\n".join(blocks)


def _pick_worker_by_embedding(*, workers: list[Agent], message: str) -> tuple[Agent | None, float]:
    message_vector = embedding_service.embed_one(str(message or ''))
    if not message_vector:
        return None, -1.0

    best_worker = None
    best_score = -1.0
    for worker in workers:
        profile_text = f"{str(worker.name or '').strip()}\n{str(worker.description or '').strip()}"
        worker_vector = embedding_service.embed_one(profile_text)
        score = _cosine_similarity(message_vector, worker_vector)
        if score > best_score:
            best_worker = worker
            best_score = score
    return best_worker, best_score


def _pick_worker_by_llm(*, workers: list[Agent], message: str) -> Agent | None:
    """目的：在規則與向量信心不足時，以模型進行最後裁決。
    為什麼：語意重述與跨領域問題僅靠關鍵字/向量可能誤判，需有第三層補強。
    """
    if not workers:
        return None
    candidate_lines = [f"- id={str(w.id)} name={str(w.name or '')} desc={str(w.description or '')}" for w in workers]
    prompt = (
        "你是路由決策器。請從候選代理者中選一個最適合處理使用者問題的 id。\n"
        "只輸出 JSON：{\"agent_id\":\"...\",\"confidence\":0~1}\n\n"
        f"[候選代理者]\n{chr(10).join(candidate_lines)}\n\n"
        f"[使用者問題]\n{str(message or '').strip()}"
    )
    try:
        llm = LLMClient()
        llm.init_for_session(session_id='route-judge', preferred_tier='cloud', overrides={'tier': 'cloud'})
        result = ''.join(list(llm.stream_complete(prompt=prompt, tier='cloud')))
        start = result.find('{')
        end = result.rfind('}')
        if start < 0 or end <= start:
            return None
        obj = _json.loads(result[start:end + 1])
        agent_id = str((obj or {}).get('agent_id') or '').strip()
        if not agent_id:
            return None
        for worker in workers:
            if str(worker.id) == agent_id:
                return worker
    except Exception:
        return None
    return None


def _pick_worker_without_default(*, workers: list[Agent], message: str) -> tuple[Agent | None, str]:
    """目的：在指定候選清單中挑選最適合代理者，未命中時不做預設回退。
    為什麼：主流程需要先嘗試 tasked，若沒有明確命中再回退 public，不可過早固定到任一 tasked。
    """
    if not workers:
        return None, 'worker_not_found'

    matched = _pick_worker_by_rules(workers=workers, message=message)
    if matched is not None:
        return matched, 'rule_match'

    embedded, score = _pick_worker_by_embedding(workers=workers, message=message)
    try:
        threshold = float(getattr(settings, 'ROUTER_EMBEDDING_THRESHOLD', 0.55))
    except Exception:
        threshold = 0.55
    threshold = max(0.0, min(1.0, threshold))
    if embedded is not None and score >= threshold:
        return embedded, 'embedding_match'

    short_message = len(str(message or '').strip()) <= 10
    if short_message:
        return None, 'short_message_no_judge'

    judged = _pick_worker_by_llm(workers=workers, message=message)
    if judged is not None:
        return judged, 'llm_judge'

    return None, 'no_confident_match'


def _pick_worker_agent(*, db: Session, router_agent: Agent, message: str) -> tuple[Agent | None, str]:
    """目的：以混合路由策略挑選工作代理者。
    為什麼：先用快路徑降低延遲，再以語意比對與模型裁決補齊準確率。
    """
    routing_message = _extract_routing_message(message)
    tasked_workers = db.query(Agent).filter(
        Agent.id != router_agent.id,
        Agent.enabled == True,  # noqa: E712
        Agent.agent_class == 'tasked',
    ).all()
    public_workers = db.query(Agent).filter(
        Agent.id != router_agent.id,
        Agent.enabled == True,  # noqa: E712
        Agent.agent_class == 'public',
    ).all()

    # 若使用者明確提到代理名稱，直接命中
    explicit = _pick_explicit_named_worker(message=routing_message, workers=(public_workers + tasked_workers))
    if explicit is not None:
        worker_class = str(getattr(explicit, 'agent_class', '') or '')
        if worker_class == 'public':
            return explicit, 'public_explicit_mention'
        if worker_class == 'tasked':
            return explicit, 'tasked_explicit_mention'
        return explicit, 'explicit_mention'

    all_worker_skill_names: list[str] = []
    for worker in (public_workers + tasked_workers):
        worker_skill_names = _extract_agent_skill_names(db=db, agent=worker)
        all_worker_skill_names.extend([str(name) for name in worker_skill_names if str(name or '').strip()])
    _log.debug(f'candidate_skill_names={all_worker_skill_names}')

    # 從候選代理綁定技能的 `name + description` 抽 token，與使用者訊息比對。
    intent_skill_name = _detect_intent_skill_name(
        db=db,
        message=routing_message,
        candidate_skill_names=all_worker_skill_names,
    )
    _log.debug(f'intent_skill_name={intent_skill_name}')

    if intent_skill_name:
        for worker in (public_workers + tasked_workers):
            names = _extract_agent_skill_names(db=db, agent=worker)
            if any(str(n).strip().lower() == intent_skill_name for n in names):
                worker_class = str(getattr(worker, 'agent_class', '') or '')
                _log.debug(f'intent_skill_matched_worker id={worker.id} class={worker_class} name={worker.name}')
                if worker_class == 'public' or worker_class == 'tasked' :
                    return worker, f'public_skill_hint_{intent_skill_name}'
                return worker, f'skill_hint_{intent_skill_name}'

    worker, reason = _pick_worker_without_default(workers=tasked_workers, message=routing_message)
    if worker is not None:
        return worker, f'tasked_{reason}'

    public_worker, public_reason = _pick_worker_without_default(workers=public_workers, message=routing_message)
    if public_worker is not None:
        return public_worker, f'public_{public_reason}'
    if public_workers:
        return public_workers[0], 'public_default_fallback'

    if tasked_workers:
        return tasked_workers[0], 'tasked_default_fallback'

    return None, 'worker_not_found'


def _extract_routing_message(message: str) -> str:
    raw_message = str(message or '').strip()
    marker = '[附加輸入]'
    marker_index = raw_message.find(marker)
    if marker_index < 0:
        return raw_message
    primary_message = raw_message[:marker_index].strip()
    if primary_message:
        return primary_message
    return raw_message


def _detect_intent_skill_name(*, db: Session, message: str, candidate_skill_names: list[str]) -> str | None:
    """目的：從訊息與技能描述動態推斷最可能技能。
    為什麼：避免在程式碼硬編技能關鍵詞，讓路由可隨技能配置演進。
    """
    normalized_message = str(message or '').strip().lower()
    if not normalized_message:
        _log.debug('detect_intent_skill_name.empty_message')
        return None

    unique_skill_names: list[str] = []
    seen_names: set[str] = set()
    for raw_name in (candidate_skill_names or []):
        name = str(raw_name or '').strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in seen_names:
            continue
        seen_names.add(lowered)
        unique_skill_names.append(name)

    _log.debug(f'detect_intent_skill_name.candidate_skills={unique_skill_names}')
    if not unique_skill_names:
        return None
    candidate_lower_names = {name.lower() for name in unique_skill_names}

    skill_rows = db.query(SkillEntry).filter(SkillEntry.name.in_(unique_skill_names)).all()

    # 先走 SKILL.md 動態規則路由（以 prompt_template 原文為主）；失敗才回退舊 token 比對。
    try:
        registry = get_skill_registry()
        rule_router = get_skill_rule_router()
        manifests = []
        included_names: set[str] = set()
        for row in skill_rows:
            skill_name = str(getattr(row, 'name', '') or '').strip()
            if not skill_name:
                continue
            included_names.add(skill_name.lower())
            prompt_markdown = str(getattr(row, 'prompt_template', '') or '').strip()
            if not prompt_markdown:
                description_text = str(getattr(row, 'description', '') or '').strip()
                prompt_markdown = f"# {skill_name}\n\n{description_text}".strip()
            manifest = registry.build_manifest_from_markdown(
                skill_md_text=prompt_markdown,
                source_id=f"db:{getattr(row, 'id', skill_name)}",
                source_path=f"db://skills/{skill_name}",
            )
            _log.debug(f'skill manifest loaded from db id={row.id} name={skill_name} description="{row.description}" manifest={manifest}')

            # 執行期欄位優先權：name/description/prompt_template 以 DB 為準。
            # 若 DB 有值，覆蓋解析結果；DB 空值時才保留解析出的內容。
            manifest.name = skill_name
            db_description = str(getattr(row, 'description', '') or '').strip()
            if db_description:
                manifest.description = db_description
            manifests.append(manifest)

        filesystem_manifests = registry.list_manifests()
        for manifest in filesystem_manifests:
            manifest_name = str(manifest.name or '').strip()
            if not manifest_name:
                continue
            lowered_name = manifest_name.lower()
            if lowered_name in included_names:
                continue
            if lowered_name not in candidate_lower_names:
                continue
            manifests.append(manifest)
            included_names.add(lowered_name)

        if not manifests:
            return None

        route_result = rule_router.match(
            message=normalized_message,
            manifests=manifests,
            candidate_skill_names=unique_skill_names,
        )
        selected_skill_name = str(route_result.get('skill_name') or '').strip()
        if selected_skill_name:
            _log.debug(
                'detect_intent_skill_name.rule_router_matched',
                selected_skill=selected_skill_name,
                score=int(route_result.get('score') or 0),
                matched_rules=route_result.get('matched_rules') or [],
            )
            return selected_skill_name
        _log.debug(
            'detect_intent_skill_name.rule_router_no_match',
            score=int(route_result.get('score') or 0),
            matched_rules=route_result.get('matched_rules') or [],
        )
    except Exception as error:
        _log.warning('detect_intent_skill_name.rule_router_failed_fallback', error=str(error))

    # 先收集所有技能的名稱 token，用於避免描述詞跨技能誤判
    all_name_tokens: set[str] = set()
    for row in skill_rows:
        name_text = str(getattr(row, 'name', '') or '').strip().lower()
        all_name_tokens.add(name_text)
        for part in re.split(r'[-_\s]+', name_text):
            if len(part.strip()) >= 2:
                all_name_tokens.add(part.strip())

    best_skill_name = None
    best_score = 0
    for row in skill_rows:
        skill_name = str(getattr(row, 'name', '') or '').strip()
        if not skill_name:
            continue
        name_tokens, desc_tokens = _extract_skill_intent_tokens(row)
        # 名稱 token 得分 x2，描述 token 得 x1
        # 描述 token 若與其他技能的名稱重疊，跳過（避免 expense-request 描述含「請假」誤判）
        if skill_name and skill_name == 'taiwan-finance-news-rss' or skill_name == 'taiwan-news-rss':
            _log.debug(f'skill "{skill_name}" name_tokens={name_tokens} desc_tokens={desc_tokens}')
        score = 0
        for token in name_tokens:
            if token in normalized_message:
                score += len(token) * 2
        if score > best_score:
            best_score = score
            best_skill_name = skill_name
        if skill_name and skill_name == 'taiwan-finance-news-rss' or skill_name == 'taiwan-news-rss':
            _log.debug(f'names token match with score {score} best_score={best_score} best_skill_name={best_skill_name}')
        score = 0
        for token in desc_tokens:
            # 若描述 token 也是其他技能的名稱詞，跳過
            if token in all_name_tokens and token not in name_tokens:
                continue
            if token in normalized_message:
                score += len(token)
        if score > best_score:
            best_score = score
            best_skill_name = skill_name
        if skill_name and skill_name == 'taiwan-finance-news-rss' or skill_name == 'taiwan-news-rss':
            _log.debug(f'desc token match with score {score} best_score={best_score} best_skill_name={best_skill_name}')

    _log.debug(f'detect_intent_skill_name.best_skill={best_skill_name} best_score={best_score}')
    return best_skill_name


def _extract_skill_intent_tokens(skill_row: SkillEntry) -> tuple[list[str], list[str]]:
    """目的：從技能名稱與描述分別抽取可匹配的意圖詞。
    為什麼：名稱詞可信度高，描述詞可能提及其他技能造成誤判，需分開處理給呼叫端加權。
    回傳：(名稱 tokens, 描述 tokens)
    """
    name_text = str(getattr(skill_row, 'name', '') or '').strip().lower()
    description_text = str(getattr(skill_row, 'description', '') or '').strip().lower()

    _log.debug(f'extract_skill_intent_tokens skill_id={skill_row.id} name="{name_text}" description="{description_text}"')

    stopwords = {
        'skill', 'skills', 'tool', 'tools', 'agent', 'html', 'zip', 'mcp',
        '功能', '技能', '工具', '代理', '處理', '提供', '支援', '系統',
    }

    def _tokenize(text: str) -> list[str]:
        token_set: set[str] = set()
        _log.debug(f'using jieba tokenize text="{text}"')
        try:
            import jieba  # type: ignore
            for piece in jieba.lcut(text):
                cleaned = str(piece or '').strip().lower()
                if len(cleaned) >= 2 and cleaned not in stopwords:
                    token_set.add(cleaned)
        except Exception:
            for match in re.findall(r'[\u4e00-\u9fff]{2,}', text):
                if match not in stopwords:
                    token_set.add(match)
            for match in re.findall(r'[a-zA-Z][a-zA-Z0-9_\-]{2,}', text):
                t = match.lower()
                if t not in stopwords:
                    token_set.add(t)
        _log.debug(f'jieba output tokenized to {token_set}')
        return sorted(token_set, key=len, reverse=True)

    # 名稱 tokens：包含完整名稱、以 - / _ 分割的各段
    name_token_set: set[str] = set()
    if name_text:
        name_token_set.add(name_text)
        for part in re.split(r'[-_\s]+', name_text):
            cleaned = part.strip()
            if len(cleaned) >= 2 and cleaned not in stopwords:
                name_token_set.add(cleaned)
        name_token_set.update(_tokenize(name_text))

    name_tokens = sorted(name_token_set, key=len, reverse=True)

    # 描述 tokens：排除已在名稱 tokens 中的詞
    desc_tokens = [t for t in _tokenize(description_text) if t not in name_token_set]

    _log.debug(f'_extract_skill_intent_tokens output skill_id={skill_row.id} name_tokens={name_tokens} desc_tokens={desc_tokens}')
    return name_tokens, desc_tokens


def _pick_explicit_named_worker(*, message: str, workers: list[Agent]) -> Agent | None:
    """目的：優先解析使用者明確指名的代理者。
    為什麼：當使用者直接指定「由某代理回覆」時，應優先遵從而非規則匹配其它部門代理。
    """
    raw_text = str(message or '').strip().lower()
    if not raw_text:
        return None

    normalized_text = re.sub(r'\s+', '', raw_text)
    for worker in workers:
        name = str(worker.name or '').strip().lower()
        if not name:
            continue
        if name in raw_text:
            return worker
        compact_name = re.sub(r'\s+', '', name)
        if compact_name and compact_name in normalized_text:
            return worker
    return None


def _pick_public_with_skill(*, db: Session, public_workers: list[Agent], skill_name: str) -> Agent | None:
    """目的：在 public 候選中挑選具備指定技能的代理。
    為什麼：技能導向任務（如 humanizer）需要優先路由到已綁定技能的 public 代理。
    """
    target = str(skill_name or '').strip().lower()
    if not target:
        return None

    for worker in public_workers:
        names = _extract_agent_skill_names(db=db, agent=worker)
        if any(str(n).strip().lower() == target for n in names):
            return worker
    return None


def _extract_agent_skill_names(*, db: Session, agent: Agent) -> list[str]:
    """目的：彙整代理綁定技能名稱（id 與名稱混用相容）。
    為什麼：部分歷史資料把 skills 直接存名稱，需兼容才能正確做技能導向路由。
    """
    names: list[str] = []

    model_cfg = agent.model_config if isinstance(agent.model_config, dict) else {}
    skill_refs = [str(x) for x in list((model_cfg or {}).get('skill_ids') or []) if x]
    if skill_refs:
        id_like: list[str] = []
        non_id_like: list[str] = []
        for ref in skill_refs:
            try:
                uuid.UUID(ref)
                id_like.append(ref)
            except Exception:
                non_id_like.append(ref)
        if id_like:
            rows = db.query(SkillEntry).filter(SkillEntry.id.in_(id_like), SkillEntry.enabled == True).all()  # noqa: E712
            names.extend([str(r.name or '').strip() for r in rows if str(r.name or '').strip()])
        if non_id_like:
            rows2 = db.query(SkillEntry).filter(SkillEntry.name.in_(non_id_like), SkillEntry.enabled == True).all()  # noqa: E712
            names.extend([str(r.name or '').strip() for r in rows2 if str(r.name or '').strip()])

    raw_skills = agent.skills if isinstance(agent.skills, list) else []
    for item in raw_skills:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict):
            name = str(item.get('name') or '').strip()
            if name:
                names.append(name)

    # 兼容 agent.skills 內直接放技能名稱。
    # 若 DB 尚未有對應技能列，但檔案系統有 SKILL.md，允許保留給動態路由使用。
    text_names = [n for n in names if n]
    if text_names:
        rows3 = db.query(SkillEntry).filter(SkillEntry.name.in_(list(dict.fromkeys(text_names))), SkillEntry.enabled == True).all()  # noqa: E712
        valid_names = {str(r.name or '').strip() for r in rows3 if str(r.name or '').strip()}
        try:
            registry_names = {
                str(manifest.name or '').strip()
                for manifest in get_skill_registry().list_manifests()
                if str(manifest.name or '').strip()
            }
        except Exception:
            registry_names = set()
        names = [n for n in names if (n in valid_names) or (n in registry_names)]

    return list(dict.fromkeys([n for n in names if n]))


def _is_agent_ready_for_chat(*, db: Session, agent: Agent) -> bool:
    """目的：檢查代理者目前是否具備可用 LLM 路由。
    為什麼：當 public 代理尚未配置完成時，需回退到 master，避免使用者收到 no_route 降級訊息。
    """
    try:
        overrides = _build_agent_overrides(agent)
        tier = str(overrides.get('tier') or '').strip().lower()
        if tier == 'cloud':
            provider = str(overrides.get('provider') or '').strip()
            model = str(overrides.get('model') or '').strip()
            return bool(provider and model)
        if tier == 'onprem':
            provider = str(overrides.get('onprem_provider') or '').strip()
            base_url = str(overrides.get('onprem_base_url') or '').strip()
            model = str(overrides.get('model') or '').strip()
            return bool(provider and base_url and model)

        provider = str(overrides.get('provider') or '').strip()
        model = str(overrides.get('model') or '').strip()
        if provider and model:
            return True
        onprem_provider = str(overrides.get('onprem_provider') or '').strip()
        onprem_base_url = str(overrides.get('onprem_base_url') or '').strip()
        return bool(onprem_provider and onprem_base_url and model)
    except Exception:
        return False


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


def _parse_openai_tool_call_block(buffer: str) -> tuple[bool, str, dict[str, Any]]:
    """目的：解析 LLM 以 OpenAI function-call 格式輸出的工具呼叫 JSON。
    為什麼：部分模型（如 gpt-5）遵從 prompt 指示，產生 {"tool_calls":[...]} 格式
    而非 [[CALL tool=...]] 格式，需要相同的偵測與執行路徑。
    只在 buffer 已含完整 JSON 時回傳 True（找到 "tool_calls" + 外層 } 且可解析）。
    """
    if '"tool_calls"' not in buffer or '"function"' not in buffer:
        return False, '', {}
    import re as _re2
    # 找到第一個 {"tool_calls":...} 結構（non-greedy 至外層 }）
    m = _re2.search(r'\{\s*"tool_calls"\s*:\s*\[[\s\S]*?\]\s*\}', buffer)
    if not m:
        return False, '', {}
    try:
        data = _json.loads(m.group(0))
        calls = data.get('tool_calls') or []
        if not calls:
            return False, '', {}
        first = calls[0]
        fn = first.get('function') or {}
        name = str(fn.get('name') or '').strip()
        args = fn.get('arguments') or {}
        if isinstance(args, str):
            try:
                args = _json.loads(args)
            except Exception:
                args = {}
        return bool(name), name, args if isinstance(args, dict) else {}
    except Exception:
        return False, '', {}


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


def _apply_tool_payload_defaults(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """目的：補齊常見工具的缺省參數，避免模型遺漏必要欄位導致整輪失敗。
    為什麼：部分模型會輸出不完整 CALL 區塊（例如未填 timezone），此處提供安全預設值。
    """
    normalized_payload = dict(payload or {})
    if tool_name == 'mcp:get_current_time' and not normalized_payload.get('timezone'):
        normalized_payload['timezone'] = 'Asia/Taipei'
    if tool_name in {'mcp:taiwan-weather', 'mcp:get_taiwan_weather_forecast'}:
        raw_location = str(
            normalized_payload.get('locationName')
            or normalized_payload.get('location')
            or normalized_payload.get('city')
            or ''
        ).strip()
        location_alias_map = {
            '台北': '臺北市',
            '台北市': '臺北市',
            '臺北': '臺北市',
        }
        normalized_location = location_alias_map.get(raw_location, raw_location)
        if not normalized_location:
            normalized_location = '臺北市'
        normalized_payload.pop('location', None)
        normalized_payload.pop('city', None)
        normalized_payload['locationName'] = normalized_location
    return normalized_payload


def _extract_attachment_context_for_payload(message: str) -> str:
    """目的：從使用者訊息抽取附加輸入區塊供工具 payload 使用。
    為什麼：上傳檔案/參考網頁/雲端檔案資訊需隨工具呼叫傳遞，避免技能看不到附件內容。
    """
    raw = str(message or '')
    marker = '[附加輸入]'
    marker_pos = raw.find(marker)
    if marker_pos < 0:
        return ''
    context_text = raw[marker_pos + len(marker):].strip()
    if not context_text:
        return ''
    has_supported_attachment = any(
        label in context_text
        for label in ('參考網頁:', 'Google 雲端檔案:', '上傳檔案:')
    )
    if not has_supported_attachment:
        return ''
    return context_text


def _inject_attachment_context_into_payload(*, payload: dict[str, Any], message: str) -> dict[str, Any]:
    """目的：將附加輸入上下文寫入工具 payload。
    為什麼：讓工具在執行時可直接取得附件相關資訊，提升技能處理一致性。
    """
    normalized_payload = dict(payload or {})
    context_text = _extract_attachment_context_for_payload(message)
    _log.debug(f'extracted_attachment_context="{context_text}" from message="{message}"')
    if not context_text:
        return normalized_payload
    normalized_payload['_attachment_context'] = context_text
    existing_text = str(normalized_payload.get('text') or '').strip()
    if not existing_text:
        normalized_payload['text'] = context_text
    return normalized_payload


def _normalize_attachment_ids(raw_attachment_ids: Any) -> list[str]:
    normalized_ids: list[str] = []
    for raw_item in list(raw_attachment_ids or []):
        normalized_item = str(raw_item or '').strip()
        if not normalized_item:
            continue
        try:
            uuid.UUID(normalized_item)
        except Exception:
            continue
        normalized_ids.append(normalized_item)
    return list(dict.fromkeys(normalized_ids))


def _inject_attachment_markdowns_into_payload(
    *,
    payload: dict[str, Any],
    attachment_markdowns: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    # 目的：把附件 markdown 分檔注入工具 payload。
    # 為什麼：附件注入需與技能解耦，任何工具都可讀取 `_attachments` 並自行決定使用方式。
    normalized_payload = dict(payload or {})
    rows = list(attachment_markdowns or [])
    if not rows:
        return normalized_payload

    normalized_payload['_attachments'] = rows
    text_fragments: list[str] = []
    for item in rows:
        markdown_text = str((item or {}).get('markdown') or '').strip()
        if not markdown_text:
            continue
        filename = str((item or {}).get('filename') or 'attachment')
        text_fragments.append(f'<附件內容 {filename}>\n{markdown_text}\n</附件內容 {filename}>')

    existing_text = str(normalized_payload.get('text') or '').strip()
    if text_fragments:
        attachment_text = '\n\n'.join(text_fragments)
        if existing_text:
            normalized_payload['text'] = f'{existing_text}\n\n{attachment_text}'
        else:
            normalized_payload['text'] = attachment_text
    return normalized_payload


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

    interaction_id_text = str((payload or {}).get('interaction_id') or '').strip()
    if interaction_id_text:
        try:
            interaction_row = db.query(SkillInteraction).filter(SkillInteraction.id == interaction_id_text).first()
        except Exception:
            interaction_row = None
        if interaction_row is None:
            return {"ok": False, "error": "interaction_not_found"}
        if str(interaction_row.tool_name or '') != str(tool_name):
            return {"ok": False, "error": "interaction_tool_mismatch"}
        if str(interaction_row.conversation_id or '') != str(conv.id):
            return {"ok": False, "error": "interaction_conversation_mismatch"}

    agent = db.query(Agent).filter(Agent.id == conv.agent_id).first()
    if agent is None:
        raise not_found_error('Agent', str(conv.agent_id))

    router = ChatRouter()
    attachment_ids = _normalize_attachment_ids((payload or {}).get('attachment_ids'))
    if not attachment_ids:
        bound_rows = chat_attachment_service.list_user_attachments(
            db=db,
            user_id=str(current_user.id),
            conversation_id=str(conv.id),
        )
        attachment_ids = [str(row.id) for row in bound_rows]
    prepared_payload = dict(payload or {})
    if attachment_ids:
        bundles = chat_attachment_service.build_markdown_bundle(
            db=db,
            user_id=str(current_user.id),
            attachment_ids=attachment_ids,
        )
        prepared_payload = _inject_attachment_markdowns_into_payload(
            payload=prepared_payload,
            attachment_markdowns=[
                {
                    'attachment_id': bundle.attachment_id,
                    'filename': bundle.filename,
                    'markdown': bundle.markdown,
                    'error': bundle.error,
                }
                for bundle in bundles
            ],
        )
    # 工具呼叫：優先走 WS 串流（若不可用則回退 HTTP），並強制白名單
    result = await router.call_tool_async(session_id=str(conv.id), tool=tool_name, payload=prepared_payload, db=db, agent_id=str(agent.id))

    try:
        result_obj = result.get('result') if isinstance(result, dict) and isinstance(result.get('result'), dict) else None
        mode_text = str((result_obj or {}).get('mode') or '').strip().lower()
        if mode_text == 'final':
            raw_assistant_text = str((result_obj or {}).get('assistant_message') or '').strip()
            assistant_text = _strip_system_reminder_text(_strip_tool_protocol_text(raw_assistant_text))
            if assistant_text:
                route_payload = {
                    'router_agent_id': str(agent.id),
                    'target_agent_id': str(agent.id),
                    'target_agent_name': str(agent.name or ''),
                    'reason': f'public_skill_hint_{tool_name}',
                }
                _write_event_part_safe(
                    db=db,
                    conversation_id=str(conv.id),
                    type_='route.decision',
                    payload=route_payload,
                )
                assistant_message = Message(
                    conversation_id=conv.id,
                    role='assistant',
                    content=assistant_text,
                    timestamp=datetime.now(),
                )
                db.add(assistant_message)
                conv.last_interacted_at = datetime.now()
                db.commit()
    except Exception:
        db.rollback()

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
                        method='tools/call', params={'name': conn_name, 'arguments': {}},
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
                        method='tools/call',
                        params={'name': conn_name, 'arguments': {}},
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

            _log.debug(f'End call_tool_async  result={res}')

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
    conversation_id: str | None = None,
    attachment_ids: list[str] | None = None,
    persist_user_message: bool = True,
    persist_message: str | None = None,  # 若提供，DB 儲存此值而非 message（避免系統注入內容洩漏到畫面）
    db: Session = Depends(get_db),  # 数据库会话依赖，用于数据库操作
    current_user: User = Depends(get_current_user),  # 当前用户依赖，获取当前登录用户信息
):
    """SSE 串流聊天：以 LLM 串流文字增量，遇到工具呼叫（[[CALL tool=...]]+JSON）即時串流工具結果。"""

    _log.debug(f'chat_stream.start agent_id={agent_id} conversation_id={conversation_id} persist_user_message={persist_user_message}')
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)
    if not bool(getattr(agent, 'enabled', True)):
        raise validation_error('代理者已停用')

    if not message or len(message.strip()) == 0:
        raise validation_error('Message cannot be empty')

    # Why: 串流與同步路徑共用同一會話/寫盤相容策略，避免切頁後資料差異。
    conversation = None
    if conversation_id:
        try:
            conversation = db.query(Conversation).filter(
                Conversation.id == conversation_id,
                Conversation.user_id == current_user.id,
                Conversation.agent_id == agent_id,
            ).first()
        except Exception:
            conversation = None
        if conversation is None:
            raise not_found_error('Conversation', conversation_id)
    if conversation is None:
        conversation = _query_latest_conversation_with_fallback(db=db, agent_id=agent_id, user_id=current_user.id)
    if not conversation:
        conversation = _create_conversation_with_fallback(db=db, agent_id=agent_id, user_id=current_user.id)

    normalized_attachment_ids = _normalize_attachment_ids(attachment_ids)
    if normalized_attachment_ids:
        chat_attachment_service.bind_attachments_to_conversation(
            db=db,
            user_id=str(current_user.id),
            attachment_ids=normalized_attachment_ids,
            conversation=conversation,
        )

    attachment_markdown_cache: list[dict[str, Any]] | None = None

    def _get_attachment_markdowns() -> list[dict[str, Any]]:
        nonlocal attachment_markdown_cache
        if attachment_markdown_cache is not None:
            return attachment_markdown_cache
        if not normalized_attachment_ids:
            attachment_markdown_cache = []
            return attachment_markdown_cache

        bundles = chat_attachment_service.build_markdown_bundle(
            db=db,
            user_id=str(current_user.id),
            attachment_ids=normalized_attachment_ids,
        )
        attachment_markdown_cache = [
            {
                'attachment_id': bundle.attachment_id,
                'filename': bundle.filename,
                'markdown': bundle.markdown,
                'error': bundle.error,
            }
            for bundle in bundles
        ]
        return attachment_markdown_cache

    user_message: Message | None = None
    if persist_user_message:
        stored_content = persist_message if persist_message is not None else message
        user_message = Message(conversation_id=conversation.id, role='user', content=stored_content)
        _save_message_with_touch_fallback(db=db, conversation=conversation, message_obj=user_message)

    router = ChatRouter()
    overrides = _build_agent_overrides(agent)
    _apply_custom_toolcall_guide(router, agent)

    try:
        router._llm.init_for_session(session_id=str(conversation.id), preferred_tier=overrides.get('tier'), overrides=overrides)
    except TypeError:
        router._llm.init_for_session(session_id=str(conversation.id), preferred_tier=overrides.get('tier'))

    agent_ctx = router._prepare_integrations(db=db, agent_id=str(agent.id))
    history_mode = str(getattr(settings, 'CHAT_HISTORY_MODE', 'recent') or 'recent').strip().lower()
    history_context = ''
    if history_mode == 'recent':
        history_context = _build_recent_history_context(
            db=db,
            conversation_id=conversation.id,
            max_messages=max(0, int(getattr(settings, 'CHAT_HISTORY_MAX_MESSAGES', 8) or 0)),
            max_tokens=max(0, int(getattr(settings, 'CHAT_HISTORY_MAX_TOKENS', 2500) or 0)),
            include_tool_text=bool(getattr(settings, 'CHAT_HISTORY_INCLUDE_TOOL_TEXT', False)),
            exclude_message_id=(user_message.id if user_message is not None else None),
        )

    _log.debug(f'chat_stream prepared agent_ctx={agent_ctx} history_context_length={len(history_context)}')

    composed_user_message = _build_capability_prompt(
        router_obj=router,
        agent_ctx=agent_ctx,
        message=message,
        history_context=history_context,
    )
    started_at = time.monotonic()
    system_prompt_snapshot = _resolve_agent_system_prompt_snapshot(agent)
    turn_status = 'success'
    turn_error: str | None = None

    _log.debug(f'chat_stream composed_user_message_length={len(composed_user_message)} system_prompt_snapshot_length={len(system_prompt_snapshot)}')

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
            # 寫入 llm_turns 審計資
            _create_llm_turn_record(
                db=db,
                conversation_id=str(conversation.id),
                agent_id=str(agent.id),
                user_message_id=(str(user_message.id) if user_message is not None else None),
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
                text = f"模型路由暫時不可用（{reason}）。請稍後重試。"
                yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(text, ensure_ascii=False)} }}\n\n"
                yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"

            _log.debug(f'chat_stream no route available, reason={reason}, route_info={route_info}, context_snapshot={context_snapshot}')
            return StreamingResponse(_gen_unavailable(), media_type='text/event-stream')

    async def _gen():
        nonlocal turn_status, turn_error
        buffer = ''
        detected_tool = False
        tool_name = ''
        tool_payload: dict[str, Any] = {}
        react_step = 0
        max_steps = int(getattr(settings, 'REACT_MAX_STEPS', 3) or 3)

        _log.debug('_gen started with message: %s', message)

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
                    _log.debug(f'After stream_complete_async _fallback_general_answer received part: {part}')
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
            return "目前工具暫時不可用。"

        def _normalize_mcp_frame(frame: dict) -> dict:
            """將標準 JSON-RPC tools/call 回應正規化為 {ok, result, _result_text} 格式。
            為什麼：MCP 標準回應沒有 ok 欄位，但現有程式碼以 ok 判斷成功/失敗。
            """
            if not isinstance(frame, dict) or 'ok' in frame:
                return frame
            if 'result' in frame:
                mcp_result = frame.get('result') or {}
                content = mcp_result.get('content') if isinstance(mcp_result, dict) else None
                if isinstance(content, list) and content:
                    result_text = ' '.join(
                        c.get('text', '') for c in content
                        if isinstance(c, dict) and c.get('type') == 'text'
                    )
                else:
                    result_text = _json.dumps(mcp_result, ensure_ascii=False)
                return {'ok': True, 'result': mcp_result, '_result_text': result_text}
            if 'error' in frame:
                err = frame.get('error')
                err_str = err.get('message') if isinstance(err, dict) else str(err or 'mcp_error')
                return {'ok': False, 'error': err_str}
            return frame

        async def _observe_and_answer(tool_nm: str, result_text: str):
            """工具執行成功後：以工具結果呼叫 LLM 產生最終回答。
            為什麼：ReAct 循環的 Observe 步驟，必須把工具輸出注回 LLM 才能形成完整答案。
            """
            _log.debug(f'_observe_and_answer called with tool_nm={tool_nm} result_text={result_text} message={message}')
            user_visible_message = _extract_routing_message(_strip_system_reminder_text(message))
            observe_prompt = (
                f"工具 {tool_nm} 已回傳以下資訊：\n{result_text}\n\n"
                f"請根據此資訊，直接用繁體中文回答使用者：{user_visible_message}\n"
                "規則：\n"
                "1) 只輸出最終答案，不可輸出中間推理、規劃、檢查過程。\n"
                "2) 不可輸出 JSON、程式碼區塊、或任何工具協定文字。\n"
                "3) 不可再呼叫任何工具。\n"
                "4) 請以簡單、重點式方式回答，若有連結請保留。"
            )
            async for obs_delta in _stream_complete_async(router._llm, prompt=observe_prompt, tier=overrides.get('tier')):
                if isinstance(obs_delta, str) and obs_delta:
                    cleaned_obs = _strip_system_reminder_text(obs_delta)
                    _log.debug(f'After _strip_system_reminder_text cleaned_obs: {cleaned_obs}, obs_delta: {obs_delta}')
                    if cleaned_obs:
                        yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(cleaned_obs, ensure_ascii=False)} }}\n\n"

        def _extract_readable_text_from_tool_result(result_obj: Any) -> str:
            """目的：從工具回傳中提取可直接回覆的文字內容。
            為什麼：若工具本身已產生最終文本，應直接回傳，避免再經 LLM 二次摘要失真。
            """
            if isinstance(result_obj, str):
                return _strip_system_reminder_text(result_obj).strip()
            if isinstance(result_obj, dict):
                direct_text = result_obj.get('text')
                if isinstance(direct_text, str) and direct_text.strip():
                    return _strip_system_reminder_text(direct_text).strip()
                nested = result_obj.get('result')
                if isinstance(nested, dict):
                    nested_text = nested.get('text')
                    if isinstance(nested_text, str) and nested_text.strip():
                        return _strip_system_reminder_text(nested_text).strip()
                content = result_obj.get('content')
                if isinstance(content, list):
                    parts = [
                        str(c.get('text') or '').strip()
                        for c in content
                        if isinstance(c, dict) and str(c.get('type') or '').strip() == 'text' and str(c.get('text') or '').strip()
                    ]
                    if parts:
                        return _strip_system_reminder_text('\n'.join(parts)).strip()
            return ''

        async def _handle_skill_mode_result(tool_name: str, tool_response: dict[str, Any]) -> tuple[bool, list[str]]:
            # 目的：統一處理技能回傳的 UI/FINAL/ERROR 模式事件。
            # 為什麼：避免多條工具呼叫分支各自重複判斷，造成行為不一致。
            nonlocal turn_status, turn_error
            event_chunks: list[str] = []

            def _append_sse_payload(payload_obj: dict[str, Any]) -> None:
                event_chunks.append(f"data: {_json.dumps(payload_obj, ensure_ascii=False)}\\n\\n")

            def _append_sse_raw(raw_text: str) -> None:
                event_chunks.append(raw_text)

            if not isinstance(tool_response, dict):
                return False, event_chunks
            if not bool(tool_response.get('ok')):
                return False, event_chunks

            result_obj = tool_response.get('result')
            if not isinstance(result_obj, dict):
                return False, event_chunks

            mode_text = str(result_obj.get('mode') or '').strip().lower()
            if mode_text == 'ui':
                interaction_id = str(result_obj.get('interaction_id') or '')
                ui_obj = result_obj.get('ui') if isinstance(result_obj.get('ui'), dict) else {}
                ui_event = {
                    'type': 'skill_ui_open',
                    'tool': tool_name,
                    'interaction_id': interaction_id,
                    'conversation_id': str(conversation.id),
                    'channel_nonce': str(result_obj.get('channel_nonce') or ''),
                    'title': str(ui_obj.get('title') or ''),
                    'ui_url': str(ui_obj.get('ui_url') or ''),
                    'entry': str(ui_obj.get('entry') or ''),
                    'state': ui_obj.get('state') if isinstance(ui_obj.get('state'), dict) else {},
                    'step': result_obj.get('step'),
                }
                _append_sse_raw(_react_event(
                    "act_result",
                    f"技能 {tool_name} 回傳互動畫面",
                    {"step": react_step, "tool": tool_name, "ok": True, "mode": "ui"},
                ))
                _append_sse_payload(ui_event)
                _append_sse_raw(f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\\n\\n")
                return True, event_chunks

            if mode_text == 'final':
                interaction_id = str(result_obj.get('interaction_id') or '')
                close_event = {
                    'type': 'skill_ui_close',
                    'tool': tool_name,
                    'interaction_id': interaction_id,
                    'conversation_id': str(conversation.id),
                }
                _append_sse_payload(close_event)
                _append_sse_raw("data: {\"type\":\"text_clear_tool\"}\\n\\n")
                assistant_message = str(result_obj.get('assistant_message') or '').strip()
                if assistant_message:
                    _append_sse_raw(f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(assistant_message, ensure_ascii=False)} }}\\n\\n")
                else:
                    output_obj = result_obj.get('output') if isinstance(result_obj.get('output'), dict) else {'output': result_obj.get('output')}
                    output_text = _json.dumps(output_obj, ensure_ascii=False)
                    async for evt in _observe_and_answer(tool_name, output_text):
                        _append_sse_raw(evt)
                _append_sse_raw(_react_event(
                    "act_result",
                    f"技能 {tool_name} 完成流程",
                    {"step": react_step, "tool": tool_name, "ok": True, "mode": "final"},
                ))
                _append_sse_raw(f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\\n\\n")
                return True, event_chunks

            if mode_text == 'error':
                error_text = str(result_obj.get('error') or 'skill_ui_error')
                turn_status = 'tool_error'
                turn_error = error_text
                interaction_id = str(result_obj.get('interaction_id') or '')
                error_event = {
                    'type': 'skill_ui_error',
                    'tool': tool_name,
                    'interaction_id': interaction_id,
                    'conversation_id': str(conversation.id),
                    'error': error_text,
                }
                _append_sse_payload(error_event)
                _append_sse_raw(_react_event(
                    "act_result",
                    f"技能 {tool_name} 流程失敗",
                    {"step": react_step, "tool": tool_name, "ok": False, "mode": "error", "error": error_text},
                ))
                fb = await _fallback_general_answer(error_text)
                _append_sse_raw(f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\\n\\n")
                _append_sse_raw(f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\\n\\n")
                return True, event_chunks

            return False, event_chunks

        def _infer_intent_skill_tool() -> str | None:
            # 目的：以技能描述動態推斷是否可直接啟動技能。
            # 為什麼：避免模型未輸出 tool call 時卡住，且不在程式碼硬編特定技能詞。
            allowed_tools = set(getattr(router, '_allowed_tools', set()) or set())
            if not allowed_tools:
                return None

            # 優先從 chat_entry_router 注入的系統提示中直接取得技能名稱
            # 避免重新跑 token 比對而選到錯誤技能
            _HINT_MARKER = '[系統提示：請優先呼叫技能 `'
            if message.startswith(_HINT_MARKER):
                rest = message[len(_HINT_MARKER):]
                end_idx = rest.find('`')
                if end_idx > 0:
                    hinted = rest[:end_idx].strip()
                    if hinted in allowed_tools:
                        return hinted

            return _detect_intent_skill_name(
                db=db,
                message=message,
                candidate_skill_names=[str(name) for name in allowed_tools],
            )

        def _should_start_skill_interaction(tool_name: str) -> bool:
            # 目的：判斷技能是否應以互動流程（action=start）啟動。
            # 為什麼：非互動技能若強制走互動封裝，會導致 payload 缺失而失敗。
            normalized_name = str(tool_name or '').strip()
            if (not normalized_name) or normalized_name.startswith('mcp:'):
                return False
            skill_row = db.query(SkillEntry).filter(SkillEntry.name == normalized_name).first()
            if skill_row is None:
                return False
            skill_type = str(getattr(skill_row, 'skill_type', 'executable') or 'executable').strip().lower()
            has_zip_bundle = bool(getattr(skill_row, 'zip_bundle', None))
            return (skill_type in {'prompt', 'hybrid'}) or has_zip_bundle

        def _can_run_intent_shortcut_payload(tool_name: str, payload: dict[str, Any], should_start_interaction: bool) -> bool:
            # 目的：驗證意圖捷徑 payload 是否滿足技能必要欄位。
            # 為什麼：避免缺少 required 參數時直接呼叫技能導致 schema_validation_failed。
            if should_start_interaction:
                return True
            normalized_name = str(tool_name or '').strip()
            if (not normalized_name) or normalized_name.startswith('mcp:'):
                return True
            skill_row = db.query(SkillEntry).filter(SkillEntry.name == normalized_name).first()
            if skill_row is None:
                return True
            input_schema = getattr(skill_row, 'input_schema', None)
            if not isinstance(input_schema, dict):
                return True
            required_fields = input_schema.get('required')
            if not isinstance(required_fields, list) or not required_fields:
                return True
            normalized_payload = payload if isinstance(payload, dict) else {}
            for field_name in required_fields:
                key = str(field_name or '').strip()
                if not key:
                    continue
                value = normalized_payload.get(key)
                if value is None:
                    return False
                if isinstance(value, str) and (not value.strip()):
                    return False
            return True

        async def _infer_intent_shortcut_payload(tool_name: str, user_message: str) -> dict[str, Any]:
            # 目的：在意圖捷徑缺參數時，利用 schema 從用戶原句補齊必要 payload。
            # 為什麼：避免直接放棄捷徑導致工具未被呼叫，同時不使用關鍵詞特判。
            normalized_name = str(tool_name or '').strip()
            if (not normalized_name) or normalized_name.startswith('mcp:'):
                return {}
            skill_row = db.query(SkillEntry).filter(SkillEntry.name == normalized_name).first()
            if skill_row is None:
                return {}
            input_schema = getattr(skill_row, 'input_schema', None)
            if not isinstance(input_schema, dict) or not input_schema:
                return {}
            prompt = (
                '你是工具參數抽取器。請依照提供的 JSON Schema，從使用者問題抽取參數。\n'
                '只輸出單一 JSON 物件，不要 markdown，不要額外文字。\n\n'
                f'工具名稱：{normalized_name}\n'
                f'JSON Schema：{_json.dumps(input_schema, ensure_ascii=False)}\n'
                f'使用者問題：{user_message}\n'
            )
            chunks: list[str] = []
            try:
                async for delta in _stream_complete_async(router._llm, prompt=prompt, tier=overrides.get('tier')):
                    if isinstance(delta, str) and delta:
                        chunks.append(delta)
                        if sum(len(part) for part in chunks) >= 1200:
                            break
            except Exception:
                return {}
            raw = ''.join(chunks).strip()
            if not raw:
                return {}
            start_idx = raw.find('{')
            end_idx = raw.rfind('}')
            if start_idx < 0 or end_idx <= start_idx:
                return {}
            try:
                payload = _json.loads(raw[start_idx:end_idx + 1])
            except Exception:
                return {}
            return payload if isinstance(payload, dict) else {}

        intent_skill_tool = _infer_intent_skill_tool()
        if intent_skill_tool:
            react_step = 1
            should_start_interaction = _should_start_skill_interaction(intent_skill_tool)
            start_payload = {'action': 'start', 'form_data': {}} if should_start_interaction else {}
            can_run_shortcut = _can_run_intent_shortcut_payload(intent_skill_tool, start_payload, should_start_interaction)
            if (not can_run_shortcut) and (not should_start_interaction):
                inferred_payload = await _infer_intent_shortcut_payload(intent_skill_tool, message)
                if isinstance(inferred_payload, dict) and inferred_payload:
                    start_payload = inferred_payload
                    can_run_shortcut = _can_run_intent_shortcut_payload(intent_skill_tool, start_payload, should_start_interaction)
            start_payload = _inject_attachment_context_into_payload(payload=start_payload, message=message)
            start_payload = _inject_attachment_markdowns_into_payload(
                payload=start_payload,
                attachment_markdowns=_get_attachment_markdowns(),
            )
            can_run_shortcut = _can_run_intent_shortcut_payload(intent_skill_tool, start_payload, should_start_interaction)
            if not can_run_shortcut:
                intent_skill_tool = None
            _log.debug(
                'intent_skill_inference',
                tool=intent_skill_tool,
                should_start_interaction=should_start_interaction,
                can_run_shortcut=can_run_shortcut,
                payload=start_payload,
            )

        if intent_skill_tool:
            yield _react_event(
                phase='plan',
                message=f'規劃第 {react_step} 步，意圖命中直接呼叫工具 {intent_skill_tool}',
                extra={'step': react_step, 'tool': intent_skill_tool, 'intent_shortcut': True},
            )
            yield "data: {\"type\":\"text_clear_tool\"}\n\n"
            yield _react_event(
                phase='act_start',
                message=f'開始執行工具 {intent_skill_tool}',
                extra={'step': react_step, 'tool': intent_skill_tool, 'intent_shortcut': True},
            )
            yield f"data: {_json.dumps({'type': 'tool_start', 'name': intent_skill_tool, 'args': start_payload}, ensure_ascii=False)}\n\n"

            _log.debug(f'Calling intent skill tool {intent_skill_tool} with payload: {start_payload}')
            res = await router.call_tool_async(
                session_id=str(conversation.id),
                tool=intent_skill_tool,
                payload=start_payload,
                db=db,
                agent_id=str(agent.id),
            )
            yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(intent_skill_tool)},\"frame\":{_json.dumps(res, ensure_ascii=False)} }}\n\n"
            handled_mode, handled_events = await _handle_skill_mode_result(intent_skill_tool, res)
            for handled_event in handled_events:
                yield handled_event
            if handled_mode:
                return

            ok_flag = bool((res or {}).get('ok', False))
            yield _react_event('act_result', f'工具 {intent_skill_tool} 已回傳結果', {'step': react_step, 'tool': intent_skill_tool, 'ok': ok_flag})
            if ok_flag:
                yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                result_obj = (res or {}).get('result', {})
                _log.debug(f'Raw tool result for {intent_skill_tool}: {result_obj}')
                direct_text = _extract_readable_text_from_tool_result(result_obj)
                result_text = direct_text if direct_text else _json.dumps(result_obj, ensure_ascii=False)
                if direct_text:
                    yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(direct_text, ensure_ascii=False)} }}\n\n"
                    yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                    return
                observed_any = False
                async for evt in _observe_and_answer(intent_skill_tool, result_text):
                    observed_any = True
                    yield evt
                if not observed_any:
                    yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(result_text, ensure_ascii=False)} }}\n\n"
                yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                return

            turn_status = 'tool_error'
            turn_error = str((res or {}).get('error') or 'skill_shortcut_failed')
            fb = await _fallback_general_answer(turn_error)
            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
            yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
            return

        async for delta in _stream_complete_async(router._llm, prompt=composed_user_message, tier=overrides.get('tier')):
            _log.debug(f'After stream_complete_async received delta: {delta}')
            if not isinstance(delta, str):
                continue
            delta = _strip_system_reminder_text(delta)
            if not delta:
                continue
            buffer += delta
            if not detected_tool:
                has_call, parsed_tool_name, parsed_payload = _parse_tool_call_block(buffer)
                # 若 [[CALL tool=...]] 未偵測到，嘗試 OpenAI function-call JSON 格式
                if not has_call:
                    has_call, parsed_tool_name, parsed_payload = _parse_openai_tool_call_block(buffer)
                if has_call:
                    tool_name = parsed_tool_name
                    tool_payload = _apply_tool_payload_defaults(parsed_tool_name, parsed_payload)
                    tool_payload = _inject_attachment_context_into_payload(payload=tool_payload, message=message)
                    tool_payload = _inject_attachment_markdowns_into_payload(
                        payload=tool_payload,
                        attachment_markdowns=_get_attachment_markdowns(),
                    )
                    _log.debug(f'Detected tool call: {tool_name} with payload: {tool_payload}')
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
                yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                yield _react_event(
                    phase="act_start",
                    message=f"開始執行工具 {tool_name}",
                    extra={"step": react_step, "tool": tool_name},
                )
                # 通知前端工具開始（附上參數以支援前端重執行）
                start_payload = {"type": "tool_start", "name": tool_name, "args": tool_payload or {}}
                yield f"data: {_json.dumps(start_payload, ensure_ascii=False)}\n\n"
                if tool_name.startswith('mcp:') and hasattr(router, '_mcp'):
                    conn_name = tool_name.split(':', 1)[1]
                    conn = getattr(router, '_mcp_map', {}).get(conn_name)
                    resolved_tool_name = conn_name
                    try:
                        resolver = getattr(router, '_resolve_mcp_tool_name', None)
                        if callable(resolver) and isinstance(conn, dict):
                            maybe_name = resolver(conn_name=conn_name, conn=conn)
                            if isinstance(maybe_name, str) and maybe_name.strip():
                                resolved_tool_name = maybe_name.strip()
                    except Exception:
                        resolved_tool_name = conn_name
                    # stdio 模式：持久 stdio 串流
                    if conn and str(conn.get('transport') or '').strip() == 'stdio':
                        cmd = str(conn.get('command') or '').strip()
                        args = conn.get('args') if isinstance(conn.get('args'), list) else []
                        env = conn.get('env') if isinstance(conn.get('env'), dict) else {}
                        tool_timeout_s = float(getattr(settings, 'MCP_TOOL_TIMEOUT_SEC', 30) or 30)
                        successful_result_text: str | None = None
                        try:
                            async with asyncio.timeout(tool_timeout_s):
                                async for frame in router._mcp.stream_rpc_call_stdio(
                                    command=cmd,
                                    args=args,
                                    env=env,
                                    method='tools/call',
                                    params={'name': resolved_tool_name, 'arguments': tool_payload or {}},
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
                                    frame = _normalize_mcp_frame(frame)
                                    _display_frame = {k: v for k, v in frame.items() if k != '_result_text'} if isinstance(frame, dict) else frame
                                    yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(_display_frame, ensure_ascii=False)} }}\n\n"
                                    ok = frame.get('ok') if isinstance(frame, dict) else None
                                    if ok is True:
                                        successful_result_text = frame.get('_result_text') or _json.dumps(frame.get('result', {}), ensure_ascii=False)
                                        break
                                    elif ok is False:
                                        turn_status = 'tool_error'
                                        turn_error = str(frame.get('error') if isinstance(frame, dict) else 'tool_error')
                                        yield _react_event("act_result", f"工具 {tool_name} 執行失敗", {"step": react_step, "tool": tool_name, "ok": False, "error": frame.get('error') if isinstance(frame, dict) else None})
                                        yield _react_event("reroute", "策略改選：工具失敗，改為一般回覆模式", {"reason_code": "tool.error", "from": tool_name, "to": "llm_fallback", "error": frame.get('error') if isinstance(frame, dict) else None})
                                        fb = await _fallback_general_answer(str(frame.get('error') if isinstance(frame, dict) else 'tool_error'))
                                        yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
                                        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                                        return
                            if successful_result_text is not None:
                                yield _react_event("act_result", f"工具 {tool_name} 執行成功", {"step": react_step, "tool": tool_name, "ok": True})
                                yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                                async for evt in _observe_and_answer(tool_name, successful_result_text):
                                    yield evt
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
                            handled_mode, handled_events = await _handle_skill_mode_result(tool_name, res)
                            for handled_event in handled_events:
                                yield handled_event
                            if handled_mode:
                                return
                            ok_flag = bool((res or {}).get('ok', False))
                            yield _react_event("act_result", f"工具 {tool_name} 已回傳結果", {"step": react_step, "tool": tool_name, "ok": ok_flag})
                            if ok_flag:
                                result_text = _json.dumps((res or {}).get('result', {}), ensure_ascii=False)
                                yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                                async for evt in _observe_and_answer(tool_name, result_text):
                                    yield evt
                                yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                                return
                    elif conn and conn.get('base_url'):
                        base_url = str(conn.get('base_url') or '').strip()
                        auth = conn.get('auth') if isinstance(conn, dict) else None
                        tool_timeout_s = float(getattr(settings, 'MCP_TOOL_TIMEOUT_SEC', 30) or 30)
                        successful_result_text: str | None = None
                        try:
                            async with asyncio.timeout(tool_timeout_s):
                                async for frame in router._mcp.stream_rpc_call_ws(
                                    base_url=base_url,
                                    method='tools/call',
                                    params={'name': resolved_tool_name, 'arguments': tool_payload or {}},
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
                                    frame = _normalize_mcp_frame(frame)
                                    _display_frame = {k: v for k, v in frame.items() if k != '_result_text'} if isinstance(frame, dict) else frame
                                    yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(_display_frame, ensure_ascii=False)} }}\n\n"
                                    ok = frame.get('ok') if isinstance(frame, dict) else None
                                    if ok is True:
                                        successful_result_text = frame.get('_result_text') or _json.dumps(frame.get('result', {}), ensure_ascii=False)
                                        break
                                    elif ok is False:
                                        turn_status = 'tool_error'
                                        turn_error = str(frame.get('error') if isinstance(frame, dict) else 'tool_error')
                                        yield _react_event("act_result", f"工具 {tool_name} 執行失敗", {"step": react_step, "tool": tool_name, "ok": False, "error": frame.get('error') if isinstance(frame, dict) else None})
                                        yield _react_event("reroute", "策略改選：工具失敗，改為一般回覆模式", {"reason_code": "tool.error", "from": tool_name, "to": "llm_fallback", "error": frame.get('error') if isinstance(frame, dict) else None})
                                        fb = await _fallback_general_answer(str(frame.get('error') if isinstance(frame, dict) else 'tool_error'))
                                        yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
                                        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                                        return
                            if successful_result_text is not None:
                                yield _react_event("act_result", f"工具 {tool_name} 執行成功", {"step": react_step, "tool": tool_name, "ok": True})
                                yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                                async for evt in _observe_and_answer(tool_name, successful_result_text):
                                    yield evt
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
                            handled_mode, handled_events = await _handle_skill_mode_result(tool_name, res)
                            for handled_event in handled_events:
                                yield handled_event
                            if handled_mode:
                                return
                            ok_flag = bool((res or {}).get('ok', False))
                            yield _react_event("act_result", f"工具 {tool_name} 已回傳結果", {"step": react_step, "tool": tool_name, "ok": ok_flag})
                            if ok_flag:
                                yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                                result_obj = (res or {}).get('result', {})
                                _log.debug(f'Raw tool result for {tool_name}: {result_obj}')
                                direct_text = _extract_readable_text_from_tool_result(result_obj)
                                result_text = direct_text if direct_text else _json.dumps(result_obj, ensure_ascii=False)
                                if direct_text:
                                    yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(direct_text, ensure_ascii=False)} }}\n\n"
                                    yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                                    return
                                observed_any = False
                                async for evt in _observe_and_answer(tool_name, result_text):
                                    observed_any = True
                                    yield evt
                                if not observed_any:
                                    yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(result_text, ensure_ascii=False)} }}\n\n"
                                yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                                return
                    else:
                        # conn 為 None 或沒有有效的 transport/URL — 工具連線未設定
                        err_msg = f'mcp_connection_not_found:{tool_name}'
                        yield _react_event("act_result", f"找不到 MCP 工具 {tool_name} 的連線設定，改為一般回覆", {"step": react_step, "tool": tool_name, "ok": False, "error": err_msg})
                        yield _react_event("reroute", "MCP 工具不可用，改為一般回覆模式", {"reason_code": "mcp.not_found", "from": tool_name, "to": "llm_fallback"})
                        # 通知前端清除已累積的工具呼叫語法，再輸出降級回覆
                        yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                        fb = await _fallback_general_answer(err_msg)
                        yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
                        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                        return
                else:
                    res = await router.call_tool_async(session_id=str(conversation.id), tool=tool_name, payload=tool_payload or {}, db=db, agent_id=str(agent.id))
                    yield f"data: {{\"type\":\"tool\",\"name\":{_json.dumps(tool_name)},\"frame\":{_json.dumps(res, ensure_ascii=False)} }}\n\n"
                    handled_mode, handled_events = await _handle_skill_mode_result(tool_name, res)
                    for handled_event in handled_events:
                        yield handled_event
                    if handled_mode:
                        return
                    ok_flag = bool((res or {}).get('ok', False))
                    yield _react_event("act_result", f"工具 {tool_name} 已回傳結果", {"step": react_step, "tool": tool_name, "ok": ok_flag})
                    if ok_flag:
                        yield "data: {\"type\":\"text_clear_tool\"}\n\n"
                        result_obj = (res or {}).get('result', {})
                        _log.debug(f'Raw tool result for {tool_name}: {result_obj}')
                        direct_text = _extract_readable_text_from_tool_result(result_obj)
                        result_text = direct_text if direct_text else _json.dumps(result_obj, ensure_ascii=False)
                        if direct_text:
                            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(direct_text, ensure_ascii=False)} }}\n\n"
                            yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                            return
                        observed_any = False
                        async for evt in _observe_and_answer(tool_name, result_text):
                            observed_any = True
                            yield evt
                        if not observed_any:
                            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(result_text, ensure_ascii=False)} }}\n\n"
                        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"
                        return
                    if not ok_flag:
                        turn_status = 'tool_error'
                        turn_error = str((res or {}).get('error') or 'tool_error')

        # 若 buffer 含未關閉/未執行的工具呼叫（[[CALL 或 tool_calls JSON），清除語法並補上降級回覆
        _has_unhandled = ('[[CALL tool=' in buffer or ('"tool_calls"' in buffer and '"function"' in buffer))
        if _has_unhandled and react_step == 0:
            yield "data: {\"type\":\"text_clear_tool\"}\n\n"
            fb = await _fallback_general_answer('incomplete_tool_call')
            yield f"data: {{\"type\":\"text\",\"delta\":{_json.dumps(fb, ensure_ascii=False)} }}\n\n"
        yield _react_event("finish", "本輪 ReAct 執行完成", {"steps": react_step})
        yield f"data: {{\"type\":\"done\",\"conversation_id\":{_json.dumps(str(conversation.id))} }}\n\n"

    # 加入帶心跳版本的包裝，以避免長時間無資料時被中間層斷線
    async def _gen_hb():
        import asyncio, time
        nonlocal turn_status, turn_error
        HEARTBEAT_SEC = 10.0
        STREAM_TIMEOUT_SEC = float(getattr(settings, 'CHAT_STREAM_TIMEOUT_SEC', 120) or 120)
        last_emit = time.monotonic()
        started_at = time.monotonic()
        queue: 'asyncio.Queue[str]' = asyncio.Queue()
        running = True
        assistant_chunks: list[str] = []

        _log.debug('_gen_hb started for conversation_id=%s', conversation.id)

        def _sanitize_text(text: str) -> str:
            out = str(text or '')
            # 目的：移除工具呼叫協定殘留，避免回覆內容被內部協定污染
            return _strip_tool_protocol_text(out)

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
                            if isinstance(payload, dict):
                                if payload.get('type') == 'text_clear_tool':
                                    assistant_chunks.clear()
                                elif payload.get('type') == 'text':
                                    d = payload.get('delta')
                                    if isinstance(d, str) and d:
                                        cleaned_delta = _strip_system_reminder_text(d)
                                        if cleaned_delta:
                                            assistant_chunks.append(cleaned_delta)
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
        _log.debug('_gen_hb setup complete, entering main loop for conversation_id=%s', conversation.id)
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
                merged = _strip_system_reminder_text(merged)
                if (not merged) and assistant_chunks:
                    merged = '系統已完成處理，但回覆內容被安全過濾。請再試一次，或改用更明確的問題。'
                assistant_message_id: str | None = None
                if merged:
                    assistant_message = Message(conversation_id=conversation.id, role='assistant', content=merged, timestamp=datetime.now())
                    db.add(assistant_message)
                    conversation.last_interacted_at = datetime.now()
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
                    user_message_id=(str(user_message.id) if user_message is not None else None),
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


# ─────────────────────────────────────────────────────────────────────────────
# 多代理 Orchestrator：輔助函式
# ─────────────────────────────────────────────────────────────────────────────



def _decompose_tasks(*, message: str, workers: list[Agent], router_agent: Agent) -> dict[str, Any] | None:
    """目的：呼叫 LLM 將訊息分解為子任務並分配給各代理者。
    為什麼：第三層（有 LLM 費用），只在 embedding 層無法明確判斷時呼叫。
    回傳 {multi: bool, tasks: [{task, agent_id, depends_on}, ...]} 或 None（失敗時降級）。
    """
    candidate_lines = [
        f"- id={str(w.id)} name={str(w.name or '')} desc={str(w.description or '')[:100]}"
        for w in workers
    ]
    prompt = (
        "你是任務分解助手。判斷以下使用者請求是否需要多個不同專長的代理者協作完成。\n"
        "可用代理者（每個有不同專長）：\n"
        f"{chr(10).join(candidate_lines)}\n\n"
        "使用者請求：\n"
        f"{message.strip()}\n\n"
        "若需要多代理（2個以上不同 agent_id），輸出 JSON：\n"
        '{"multi": true, "tasks": [{"task": "子任務描述", "agent_id": "uuid", "depends_on": null}, ...]}\n'
        "若單代理即可，輸出：\n"
        '{"multi": false, "agent_id": "uuid"}\n'
        "規則：tasks 最多 3 個；depends_on 為前置任務索引陣列或 null；只輸出 JSON，不要其他說明。"
    )
    try:
        overrides = _build_agent_overrides(router_agent)
        tier = overrides.get('tier') or 'cloud'
        llm = LLMClient()
        llm.init_for_session(session_id='orchestrator-decompose', preferred_tier=tier, overrides=overrides)
        result = ''.join(list(llm.stream_complete(prompt=prompt, tier=tier)))
        start = result.find('{')
        end = result.rfind('}')
        if start < 0 or end <= start:
            return None
        parsed = _json.loads(result[start:end + 1])
        if not isinstance(parsed, dict):
            return None
        return parsed
    except Exception as e:
        _log.warning('orchestrator.decompose_failed', error=str(e))
        return None


def _build_execution_waves(tasks: list[MultiAgentTask]) -> list[list[MultiAgentTask]]:
    """目的：依 depends_on 拓撲排序，產生執行波次（同波次內可並行）。
    為什麼：支援子任務間的依賴關係，Phase 1 每波次只有一個任務（循序）。
    """
    remaining = list(range(len(tasks)))
    completed: set[int] = set()
    waves: list[list[MultiAgentTask]] = []

    while remaining:
        wave_indices = []
        for i in remaining[:]:
            deps = tasks[i].depends_on or []
            if all(d in completed for d in deps):
                wave_indices.append(i)
                remaining.remove(i)
        if not wave_indices:
            # 循環依賴安全回退：全部循序執行
            wave_indices = remaining[:]
            remaining = []
        waves.append([tasks[i] for i in wave_indices])
        completed.update(wave_indices)

    return waves


def _inject_prior_context(*, task_desc: str, prior_results: list[dict[str, Any]]) -> str:
    """目的：將前置任務結果以結構化前綴注入當前子任務描述。
    為什麼：讓後置子代理能閱讀前一步的輸出，實現跨代理上下文傳遞。
    """
    if not prior_results:
        return task_desc
    ctx_lines = []
    for r in prior_results:
        name = str(r.get('agent_name') or '')
        text = str(r.get('result_text') or '')[:2000]
        if text.strip():
            ctx_lines.append(f"[前置任務結果 - {name}]\n{text}")
    if not ctx_lines:
        return task_desc
    ctx_block = '\n\n'.join(ctx_lines)
    return f"[背景資訊]\n{ctx_block}\n\n[使用者原始需求]\n{task_desc}"


async def _run_subtask_stream(
    *,
    task_row: MultiAgentTask,
    enriched_message: str,
    agent: Agent,
    db: Session,
    current_user: User,
    task_index: int,
    total_tasks: int,
    completed_so_far: int,
) -> AsyncGenerator[str, None]:
    """目的：執行單個子任務，將現有 chat_stream SSE 轉譯為 agent.* 事件。
    為什麼：複用既有 ReAct/工具呼叫邏輯，只在包裝層加上 agent 標識與 DB 寫入。
    """
    from datetime import datetime as _dt

    agent_id = str(agent.id)
    agent_name = str(agent.name or agent_id)
    overall_progress = int(completed_so_far / max(total_tasks, 1) * 100)

    # 子代理開始
    task_row.status = 'running'
    task_row.started_at = _dt.utcnow()
    try:
        db.commit()
    except Exception:
        db.rollback()

    yield f"data: {_json.dumps({'type': 'agent.start', 'agent_id': agent_id, 'agent_name': agent_name, 'task_index': task_index, 'task': enriched_message[:200], 'overall_progress': overall_progress}, ensure_ascii=False)}\n\n"

    # 呼叫現有 chat_stream
    sub_response = await chat_stream(
        agent_id=agent_id,
        message=enriched_message,
        db=db,
        current_user=current_user,
    )

    result_chunks: list[str] = []
    ok = True
    error_msg: str | None = None

    async for raw_chunk in sub_response.body_iterator:
        if not isinstance(raw_chunk, (str, bytes)):
            continue
        if isinstance(raw_chunk, bytes):
            raw_chunk = raw_chunk.decode('utf-8', errors='replace')
        raw_events = [chunk for chunk in str(raw_chunk).split('\n\n') if chunk.strip()]
        for event_text in raw_events:
            normalized_event = event_text.strip()
            if not normalized_event.startswith('data: '):
                continue
            try:
                event_payload = _json.loads(normalized_event[6:].strip())
            except Exception:
                continue

            ptype = (event_payload or {}).get('type')

            if ptype == 'text':
                delta = event_payload.get('delta', '')
                if delta:
                    result_chunks.append(delta)
                yield f"data: {_json.dumps({'type': 'agent.text', 'agent_id': agent_id, 'agent_name': agent_name, 'task_index': task_index, 'delta': delta}, ensure_ascii=False)}\n\n"
            elif ptype == 'done':
                pass  # 由 orchestrator 統一發 done
            elif ptype == 'error':
                ok = False
                error_msg = event_payload.get('message') or 'subtask_error'
            elif ptype in ('react', 'tool_start', 'tool', 'progress', 'heartbeat', 'skill_ui_open', 'skill_ui_close', 'skill_ui_error'):
                # route.decision 屬子代理內部路由，不對外轉發（避免覆蓋 orchestrator.plan UI）
                event_payload['agent_id'] = agent_id
                event_payload['agent_name'] = agent_name
                event_payload['task_index'] = task_index
                yield f"data: {_json.dumps(event_payload, ensure_ascii=False)}\n\n"

    # 寫入結果
    task_row.result_text = ''.join(result_chunks)
    task_row.finished_at = _dt.utcnow()
    new_progress = int((completed_so_far + 1) / max(total_tasks, 1) * 100)

    if ok:
        task_row.status = 'done'
        try:
            db.commit()
        except Exception:
            db.rollback()
        yield f"data: {_json.dumps({'type': 'agent.done', 'agent_id': agent_id, 'agent_name': agent_name, 'task_index': task_index, 'ok': True, 'result_preview': task_row.result_text[:300], 'overall_progress': new_progress}, ensure_ascii=False)}\n\n"
    else:
        task_row.status = 'failed'
        task_row.error = error_msg
        try:
            db.commit()
        except Exception:
            db.rollback()
        yield f"data: {_json.dumps({'type': 'agent.error', 'agent_id': agent_id, 'agent_name': agent_name, 'task_index': task_index, 'error': error_msg, 'degradation': 'skip', 'overall_progress': new_progress}, ensure_ascii=False)}\n\n"


async def _synthesize_and_evaluate(
    *,
    original_message: str,
    task_rows: list[MultiAgentTask],
    router_agent: Agent,
    overrides: dict[str, Any],
) -> tuple[str, bool, str]:
    """目的：Router LLM 合成所有子代理結果，再自評是否滿足需求。
    為什麼：合成確保回覆完整一致；自評是 Orchestrator ReAct 的 Observe 步驟，決定是否重試。
    回傳 (synthesis_text, eval_ok, eval_reason)。
    """
    # 收集子代理結果
    results_block_lines: list[str] = []
    for t in task_rows:
        if t.status in ('done',) and t.result_text:
            results_block_lines.append(f"[{str(t.task_desc or '')[:80]}]\n{str(t.result_text or '')[:2000]}")
        elif t.status in ('failed', 'skipped'):
            results_block_lines.append(f"[{str(t.task_desc or '')[:80]}]\n（此子任務未完成：{str(t.error or t.status)}）")

    results_block = '\n\n'.join(results_block_lines) if results_block_lines else '（無可用子代理結果）'

    synth_prompt = (
        f"你是協調助理，負責整合多個子代理的工作成果並回覆使用者。\n\n"
        f"[使用者原始需求]\n{original_message.strip()}\n\n"
        f"[子代理工作結果]\n{results_block}\n\n"
        "請根據以上資訊，整合成一份完整、清晰的回覆給使用者。"
        "若有子任務未完成，請說明原因並提供已完成的部分。"
    )

    llm = LLMClient()
    try:
        llm.init_for_session(
            session_id='orchestrator-synthesize',
            preferred_tier=overrides.get('tier'),
            overrides=overrides,
        )
    except TypeError:
        llm.init_for_session(
            session_id='orchestrator-synthesize',
            preferred_tier=overrides.get('tier'),
        )

    synthesis_chunks: list[str] = []
    try:
        async for delta in _stream_complete_async(llm, prompt=synth_prompt, tier=overrides.get('tier')):
            _log.debug('synthesis delta: %s', delta)
            if isinstance(delta, str) and delta:
                synthesis_chunks.append(delta)
    except Exception as e:
        _log.warning('orchestrator.synthesize_failed', error=str(e))

    synthesis = ''.join(synthesis_chunks).strip()
    if not synthesis:
        synthesis = '抱歉，合成回覆時發生問題，以下為各子代理的原始結果：\n' + results_block

    # 自評
    eval_prompt = (
        "請判斷以下回覆是否完整回應了使用者的需求。\n\n"
        f"[使用者原始需求]\n{original_message.strip()}\n\n"
        f"[回覆內容]\n{synthesis[:3000]}\n\n"
        "只輸出 JSON，不要其他說明：\n"
        '{"ok": true/false, "reason": "簡短說明"}'
    )

    eval_ok = True
    eval_reason = ''
    try:
        eval_llm = LLMClient()
        eval_llm.init_for_session(
            session_id='orchestrator-evaluate',
            preferred_tier='cloud',
            overrides={'tier': 'cloud'},
        )
        import asyncio as _asyncio
        eval_result = await _asyncio.to_thread(
            lambda: ''.join(list(eval_llm.stream_complete(prompt=eval_prompt, tier='cloud')))
        )
        s = eval_result.find('{')
        e = eval_result.rfind('}')
        if s >= 0 and e > s:
            obj = _json.loads(eval_result[s:e + 1])
            eval_ok = bool(obj.get('ok', True))
            eval_reason = str(obj.get('reason') or '')
    except Exception as ex:
        _log.warning('orchestrator.evaluate_failed', error=str(ex))
        eval_ok = True  # 自評失敗時預設通過，避免無限重試

    return synthesis, eval_ok, eval_reason


async def _multi_agent_orchestrator(
    *,
    message: str,
    plan: dict[str, Any],
    workers: list[Agent],
    db: Session,
    current_user: User,
    router_agent: Agent,
    conversation: Conversation,
) -> AsyncGenerator[str, None]:
    """目的：Orchestrator ReAct 主循環：分解 → 執行子代理 → 合成 → 自評 → 必要時重試。
    為什麼：單一協調點確保任務可重試、結果可觀測、SSE 流統一。
    """
    from datetime import datetime as _dt

    def _sanitize(text: str) -> str:
        """移除 LLM 工具呼叫協議殘留，避免存入 DB 的訊息含內部標記。"""
        return _strip_tool_protocol_text(text)

    overrides = _build_agent_overrides(router_agent)
    max_steps = int(getattr(settings, 'ORCHESTRATOR_MAX_STEPS', 3) or 3)

    # 建立 MultiAgentSession
    session = MultiAgentSession(
        conversation_id=conversation.id,
        router_agent_id=router_agent.id,
        user_message=message,
        status='planning',
        react_step=0,
        max_steps=max_steps,
        plan_json=plan,
    )
    db.add(session)
    try:
        db.commit()
        db.refresh(session)
    except Exception:
        db.rollback()

    # 快取純 Python 值，後續 raw SQL 不再碰 ORM 物件（避免 expired 問題）
    from sqlalchemy import text as _sa_text
    _session_id = str(session.id)
    _conv_id = str(conversation.id)

    current_plan = plan
    completed_so_far = 0

    for step in range(max_steps):
        try:
            db.execute(_sa_text(
                "UPDATE multi_agent_sessions SET react_step=:s, status='running', plan_json=:p WHERE id=:sid"
            ), {"s": step + 1, "p": _json.dumps(current_plan), "sid": _session_id})
            db.commit()
        except Exception:
            db.rollback()

        # 建立本輪子任務列
        raw_tasks: list[dict[str, Any]] = (current_plan or {}).get('tasks') or []
        # 找出各 agent 物件
        worker_map: dict[str, Agent] = {str(w.id): w for w in workers}
        task_rows: list[MultiAgentTask] = []
        for idx, t in enumerate(raw_tasks):
            agent_id_str = str(t.get('agent_id') or '')
            target_agent = worker_map.get(agent_id_str)
            if target_agent is None:
                continue
            tr = MultiAgentTask(
                session_id=_session_id,
                task_index=idx,
                agent_id=target_agent.id,
                task_desc=str(t.get('task') or message),
                depends_on=t.get('depends_on') or [],
                status='pending',
            )
            db.add(tr)
            task_rows.append(tr)
        try:
            db.commit()
        except Exception:
            db.rollback()

        if not task_rows:
            break

        # 發出計畫事件
        plan_event = {
            'type': 'orchestrator.plan',
            'react_step': step + 1,
            'total_tasks': len(task_rows),
            'tasks': [
                {
                    'index': tr.task_index,
                    'task': str(tr.task_desc or '')[:200],
                    'agent_id': str(tr.agent_id),
                    'agent_name': str(worker_map.get(str(tr.agent_id), router_agent).name or ''),
                    'depends_on': tr.depends_on or [],
                }
                for tr in task_rows
            ],
        }
        yield f"data: {_json.dumps(plan_event, ensure_ascii=False)}\n\n"

        # 執行各波次（Phase 1：循序）
        waves = _build_execution_waves(task_rows)
        prior_results: dict[int, dict[str, Any]] = {}

        for wave in waves:
            for task_row in wave:
                idx = task_row.task_index
                target_agent = worker_map.get(str(task_row.agent_id))
                if target_agent is None:
                    task_row.status = 'skipped'
                    task_row.error = 'agent_not_found'
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                    continue

                # 注入前置任務結果
                deps = task_row.depends_on or []
                prior_list = [prior_results[d] for d in deps if d in prior_results]
                enriched = _inject_prior_context(task_desc=str(task_row.task_desc), prior_results=prior_list)

                async for event_str in _run_subtask_stream(
                    task_row=task_row,
                    enriched_message=enriched,
                    agent=target_agent,
                    db=db,
                    current_user=current_user,
                    task_index=idx,
                    total_tasks=len(task_rows),
                    completed_so_far=completed_so_far,
                ):
                    yield event_str

                prior_results[idx] = {
                    'agent_name': str(target_agent.name or ''),
                    'result_text': str(task_row.result_text or ''),
                    'ok': task_row.status == 'done',
                }
                if task_row.status == 'done':
                    completed_so_far += 1

        # 合成階段
        try:
            db.execute(_sa_text(
                "UPDATE multi_agent_sessions SET status='synthesizing' WHERE id=:sid"
            ), {"sid": _session_id})
            db.commit()
        except Exception:
            db.rollback()

        yield f"data: {_json.dumps({'type': 'orchestrator.synthesizing', 'react_step': step + 1}, ensure_ascii=False)}\n\n"

        synthesis, eval_ok, eval_reason = await _synthesize_and_evaluate(
            original_message=message,
            task_rows=task_rows,
            router_agent=router_agent,
            overrides=overrides,
        )

        # 串流合成文字
        yield f"data: {_json.dumps({'type': 'agent.text', 'agent_id': str(router_agent.id), 'agent_name': str(router_agent.name or 'Router'), 'task_index': -1, 'delta': synthesis}, ensure_ascii=False)}\n\n"

        try:
            db.execute(_sa_text(
                "UPDATE multi_agent_sessions SET synthesis=:syn, eval_ok=:ok, status='evaluating' WHERE id=:sid"
            ), {"syn": synthesis, "ok": eval_ok, "sid": _session_id})
            db.commit()
        except Exception:
            db.rollback()

        if eval_ok:
            # 全用 raw SQL，不碰任何可能 expired 的 ORM 物件
            try:
                _sanitized = _sanitize(synthesis)
                db.execute(_sa_text(
                    "UPDATE multi_agent_sessions SET status='done', synthesis=:syn, eval_ok=TRUE "
                    "WHERE id=:sid"
                ), {"syn": synthesis, "sid": _session_id})
                if _sanitized:
                    import uuid as _uuid_mod
                    db.execute(_sa_text(
                        "INSERT INTO messages (id, conversation_id, role, content, timestamp) "
                        "VALUES (:id, :cid, 'assistant', :content, CURRENT_TIMESTAMP)"
                    ), {"id": str(_uuid_mod.uuid4()), "cid": _conv_id, "content": _sanitized})
                db.execute(_sa_text(
                    "UPDATE conversations SET last_interacted_at=CURRENT_TIMESTAMP WHERE id=:cid"
                ), {"cid": _conv_id})
                db.commit()
                _log.info('orchestrator.save_synthesis_ok', conversation_id=_conv_id)
            except Exception as _save_err:
                _log.error('orchestrator.save_synthesis_failed', error=str(_save_err), conversation_id=_conv_id)
                db.rollback()
            yield f"data: {_json.dumps({'type': 'orchestrator.done', 'conversation_id': _conv_id, 'completed': completed_so_far, 'failed': sum(1 for t in task_rows if t.status == 'failed'), 'react_steps_used': step + 1}, ensure_ascii=False)}\n\n"
            return

        # 不滿足 → 重新規劃
        yield f"data: {_json.dumps({'type': 'orchestrator.retry', 'react_step': step + 1, 'reason': eval_reason}, ensure_ascii=False)}\n\n"
        new_plan = _decompose_tasks(message=message, workers=workers, router_agent=router_agent)
        if not new_plan or not new_plan.get('multi'):
            break
        current_plan = new_plan
        completed_so_far = 0

    # 超過步驟上限 or 分解失敗 — 全用 raw SQL
    _fallback_text = '抱歉，多代理協作未能在步驟上限內完成，請稍後再試或簡化問題。'
    try:
        # 先查 synthesis 欄位（避免碰 expired ORM）
        _row = db.execute(_sa_text(
            "SELECT synthesis FROM multi_agent_sessions WHERE id=:sid"
        ), {"sid": _session_id}).fetchone()
        _saved_synthesis = (_row[0] if _row else None) or ''
    except Exception:
        _saved_synthesis = ''

    if not _saved_synthesis:
        _saved_synthesis = _fallback_text
        yield f"data: {_json.dumps({'type': 'agent.text', 'agent_id': str(router_agent.id), 'agent_name': str(router_agent.name or 'Router'), 'task_index': -1, 'delta': _fallback_text}, ensure_ascii=False)}\n\n"

    try:
        import uuid as _uuid_mod
        db.execute(_sa_text(
            "UPDATE multi_agent_sessions SET status='failed' WHERE id=:sid"
        ), {"sid": _session_id})
        db.execute(_sa_text(
            "INSERT INTO messages (id, conversation_id, role, content, timestamp) "
            "VALUES (:id, :cid, 'assistant', :content, CURRENT_TIMESTAMP)"
        ), {"id": str(_uuid_mod.uuid4()), "cid": _conv_id, "content": _sanitize(_saved_synthesis)})
        db.execute(_sa_text(
            "UPDATE conversations SET last_interacted_at=CURRENT_TIMESTAMP WHERE id=:cid"
        ), {"cid": _conv_id})
        db.commit()
    except Exception as _fe:
        _log.error('orchestrator.save_fallback_failed', error=str(_fe))
        db.rollback()

    yield f"data: {_json.dumps({'type': 'orchestrator.done', 'conversation_id': _conv_id, 'completed': completed_so_far, 'failed': 0, 'react_steps_used': max_steps, 'max_steps_reached': True}, ensure_ascii=False)}\n\n"


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
    enriched_message = _inject_reference_content(message)
    attachment_ids = _normalize_attachment_ids((payload or {}).get('attachment_ids'))

    requested_conversation_id = str((payload or {}).get('conversation_id') or '').strip()
    requested_conversation: Conversation | None = None
    if requested_conversation_id:
        _validate_uuid_or_not_found('Conversation', requested_conversation_id)
        requested_conversation = db.query(Conversation).filter(
            Conversation.id == requested_conversation_id,
            Conversation.user_id == current_user.id,
        ).first()
        if requested_conversation is None:
            raise not_found_error('Conversation', requested_conversation_id)

    router_agent = db.query(Agent).filter(Agent.is_router == True, Agent.enabled == True).first()  # noqa: E712
    if router_agent is None:
        router_agent = db.query(Agent).filter(Agent.enabled == True).first()  # noqa: E712
    if router_agent is None:
        raise validation_error('尚未建立可用代理者')

    # ── 多代理協作路徑（三層判斷後交 Orchestrator 處理）──
    workers = db.query(Agent).filter(
        Agent.id != router_agent.id,
        Agent.enabled == True,  # noqa: E712
        Agent.agent_class.in_(['tasked', 'public']),
    ).all()

    #判斷：multi：進多代理 orchestrator，single：走單代理挑選 _pick_worker_agent
    routing = _classify_routing(message=enriched_message, workers=workers)
    debug_trace_enabled = bool(getattr(settings, 'CHAT_DEBUG_TRACE', False)) or bool((payload or {}).get('debug'))
    if routing == 'multi':
        import asyncio as _asyncio
        import functools as _functools

        async def _orchestrate_stream() -> AsyncGenerator[str, None]:
            _decompose_started_at = time.monotonic()
            _log.debug('orchestrator_start multi start', user_message=message, router_agent_id=str(router_agent.id), worker_count=len(workers), debug=debug_trace_enabled)
            # 立即發送第一個事件，讓前端知道已進入多代理模式
            yield f"data: {_json.dumps({'type': 'orchestrator.thinking', 'message': '正在分析任務分派…'}, ensure_ascii=False)}\n\n"
            if debug_trace_enabled:
                yield f"data: {_json.dumps({'type': 'debug', 'phase': 'routing', 'routing': routing, 'workers': len(workers)}, ensure_ascii=False)}\n\n"
            # 在 thread 裡做 LLM decompose，不阻塞 event loop
            try:
                plan = await _asyncio.wait_for(
                    _asyncio.to_thread(
                        _functools.partial(_decompose_tasks, message=message, workers=workers, router_agent=router_agent)
                    ),
                    timeout=30.0,
                )
            except _asyncio.TimeoutError:
                plan = None

            if debug_trace_enabled:
                yield f"data: {_json.dumps({'type': 'debug', 'phase': 'decompose', 'elapsed_ms': int((time.monotonic() - _decompose_started_at) * 1000), 'plan': plan if isinstance(plan, dict) else None}, ensure_ascii=False)}\n\n"

            if plan and plan.get('multi') and len(plan.get('tasks', [])) >= 2:
                conversation = requested_conversation
                if conversation is None:
                    conversation = _query_latest_conversation_with_fallback(
                        db=db, agent_id=str(router_agent.id), user_id=current_user.id
                    )
                if conversation is None:
                    conversation = _create_conversation_with_fallback(
                        db=db, agent_id=str(router_agent.id), user_id=current_user.id
                    )
                if attachment_ids:
                    chat_attachment_service.bind_attachments_to_conversation(
                        db=db,
                        user_id=str(current_user.id),
                        attachment_ids=attachment_ids,
                        conversation=conversation,
                    )
                try:
                    user_msg = Message(conversation_id=conversation.id, role='user', content=enriched_message)
                    _save_message_with_touch_fallback(db=db, conversation=conversation, message_obj=user_msg)
                except Exception:
                    pass
                async for event in _multi_agent_orchestrator(
                    message=enriched_message,
                    plan=plan,
                    workers=workers,
                    db=db,
                    current_user=current_user,
                    router_agent=router_agent,
                    conversation=conversation,
                ):
                    yield event
            else:
                # decompose 失敗或降級 → 走單代理選擇器，不直接固定主代理
                worker, route_reason = _pick_worker_agent(db=db, router_agent=router_agent, message=enriched_message)
                if worker is None:
                    worker = router_agent
                    route_reason = 'decompose_failed_worker_not_found'
                else:
                    route_reason = f"decompose_failed_{route_reason}"
                worker_class = str(getattr(worker, 'agent_class', '') or '')
                if worker_class in {'public', 'tasked'}:
                    if not _is_agent_ready_for_chat(db=db, agent=worker):
                        worker = router_agent
                        route_reason = f'decompose_failed_{worker_class}_not_ready_master_fallback'
                route_payload = {
                    'type': 'route.decision',
                    'target_agent_id': str(worker.id),
                    'target_agent_name': str(worker.name or ''),
                    'reason': route_reason,
                    'degraded': True,
                }
                if debug_trace_enabled:
                    route_payload['debug'] = {'routing': routing, 'decompose_plan': plan}
                yield f"data: {_json.dumps(route_payload, ensure_ascii=False)}\n\n"
                degrade_conversation = requested_conversation
                if degrade_conversation is None:
                    degrade_conversation = _query_latest_conversation_with_fallback(
                        db=db, agent_id=str(worker.id), user_id=current_user.id
                    )
                if degrade_conversation is None:
                    degrade_conversation = _create_conversation_with_fallback(
                        db=db, agent_id=str(worker.id), user_id=current_user.id
                    )
                if attachment_ids:
                    chat_attachment_service.bind_attachments_to_conversation(
                        db=db,
                        user_id=str(current_user.id),
                        attachment_ids=attachment_ids,
                        conversation=degrade_conversation,
                    )
                if str(getattr(degrade_conversation, 'agent_id', '') or '') != str(worker.id):
                    degrade_conversation.agent_id = worker.id
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                routed_response = await chat_stream(
                    agent_id=str(worker.id),
                    message=enriched_message,
                    conversation_id=str(degrade_conversation.id),
                    attachment_ids=attachment_ids,
                    db=db,
                    current_user=current_user,
                )
                async for event in routed_response.body_iterator:
                    yield event

        return StreamingResponse(_orchestrate_stream(), media_type='text/event-stream')

    # ── 單代理路徑（原有邏輯不變）──

    _log.debug('single_agent_routing', user_message=message, router_agent_id=str(router_agent.id), debug=debug_trace_enabled)
    worker, route_reason = _pick_worker_agent(db=db, router_agent=router_agent, message=enriched_message)
    is_fallback = False
    if worker is None:
        worker = router_agent
        is_fallback = True
        route_reason = 'worker_not_found_fallback'
    elif route_reason in {'default_fallback'}:
        is_fallback = True
    worker_class = str(getattr(worker, 'agent_class', '') or '')
    if worker_class in {'public', 'tasked'}:
        if not _is_agent_ready_for_chat(db=db, agent=worker):
            worker = router_agent
            is_fallback = True
            route_reason = f'{worker_class}_not_ready_master_fallback'

    conversation = requested_conversation
    if conversation is None:
        conversation = _query_latest_conversation_with_fallback(db=db, agent_id=str(worker.id), user_id=current_user.id)
    if conversation is None:
        conversation = _create_conversation_with_fallback(db=db, agent_id=str(worker.id), user_id=current_user.id)
    if attachment_ids:
        chat_attachment_service.bind_attachments_to_conversation(
            db=db,
            user_id=str(current_user.id),
            attachment_ids=attachment_ids,
            conversation=conversation,
        )

    if str(getattr(conversation, 'agent_id', '') or '') != str(worker.id):
        conversation.agent_id = worker.id
        try:
            db.commit()
        except Exception:
            db.rollback()

    route_decision_payload = {
        'router_agent_id': str(router_agent.id),
        'target_agent_id': str(worker.id),
        'target_agent_name': worker.name,
        'reason': route_reason,
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
            payload={'reason': route_reason, 'target_agent_id': str(worker.id)},
        )

    # 若路由偵測到技能意圖，將技能名稱注入訊息讓 Worker LLM 知道要呼叫哪個技能
    # persist_message 保留原始訊息存 DB，避免系統提示內容顯示在聊天畫面
    # 同時涵蓋 tasked_skill_hint_ 與 public_skill_hint_ 兩種路由原因
    _HINT_PREFIXES = ('tasked_skill_hint_', 'public_skill_hint_')
    llm_message = enriched_message
    for _prefix in _HINT_PREFIXES:
        if route_reason and route_reason.startswith(_prefix):
            hinted_skill = route_reason[len(_prefix):]
            llm_message = (
                f'[系統提示：請優先呼叫技能 `{hinted_skill}` 來處理此請求，不要只用文字回覆。若目前只有附件檔名而沒有可讀文字內容，請先明確要求使用者貼上內容或提供可讀取連結，不要直接拒絕。]\n\n'
                + enriched_message
            )
            break

    _log.debug('Before chat_stream: final_routing_decision', user_message=message, router_agent_id=str(router_agent.id), worker_agent_id=str(worker.id), route_reason=route_reason, debug=debug_trace_enabled)
    routed_response = await chat_stream(
        agent_id=str(worker.id),
        message=llm_message,
        persist_message=enriched_message,
        conversation_id=str(conversation.id),
        attachment_ids=attachment_ids,
        persist_user_message=True,
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


@router.post('/chat/attachments')
async def upload_chat_attachment(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：以二進位方式上傳聊天附件並回傳 attachment_id。
    # 為什麼：聊天主訊息只需帶 attachment_id，工具執行時再按需轉換內容。
    _require_chat_permission(current_user)
    normalized_conversation_id = str(conversation_id or '').strip() or None
    if normalized_conversation_id:
        _validate_uuid_or_not_found('Conversation', normalized_conversation_id)
        conversation = db.query(Conversation).filter(
            Conversation.id == normalized_conversation_id,
            Conversation.user_id == current_user.id,
        ).first()
        if conversation is None:
            raise not_found_error('Conversation', normalized_conversation_id)

    try:
        row = chat_attachment_service.create_attachment(
            db=db,
            user_id=str(current_user.id),
            file=file,
            conversation_id=normalized_conversation_id,
        )
    except ValueError as error:
        raise validation_error(str(error))

    return {
        'ok': True,
        'attachment': chat_attachment_service.serialize_attachment(row),
    }


@router.get('/chat/attachments')
async def list_chat_attachments(
    conversation_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_chat_permission(current_user)
    normalized_conversation_id = str(conversation_id or '').strip() or None
    if normalized_conversation_id:
        _validate_uuid_or_not_found('Conversation', normalized_conversation_id)
        conversation = db.query(Conversation).filter(
            Conversation.id == normalized_conversation_id,
            Conversation.user_id == current_user.id,
        ).first()
        if conversation is None:
            raise not_found_error('Conversation', normalized_conversation_id)
    rows = chat_attachment_service.list_user_attachments(
        db=db,
        user_id=str(current_user.id),
        conversation_id=normalized_conversation_id,
    )
    return {
        'attachments': [chat_attachment_service.serialize_attachment(row) for row in rows],
    }


@router.get('/chat/attachments/{attachment_id}')
async def get_chat_attachment(
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_chat_permission(current_user)
    _validate_uuid_or_not_found('Attachment', attachment_id)
    row = chat_attachment_service.get_user_attachment(
        db=db,
        user_id=str(current_user.id),
        attachment_id=attachment_id,
    )
    if row is None:
        raise not_found_error('Attachment', attachment_id)
    return {
        'ok': True,
        'attachment': chat_attachment_service.serialize_attachment(row),
    }


@router.get('/chat/google-picker-config')
async def get_google_picker_config(
    current_user: User = Depends(get_current_user),
):
    """目的：提供聊天頁 Google Picker 所需公開設定。
    為什麼：前端由後端環境變數統一下發，避免每位使用者手動輸入。
    """
    _require_chat_permission(current_user)
    client_id = str(getattr(settings, 'GOOGLE_CLIENT_ID', '') or '').strip()
    api_key = str(getattr(settings, 'GOOGLE_API_KEY', '') or '').strip()
    return {
        'ok': True,
        'configured': bool(client_id and api_key),
        'google_client_id': client_id,
        'google_api_key': api_key,
    }


@router.get('/conversations')
async def get_all_conversations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """回傳目前使用者的所有對話（不限 agent），供 auto-routing 模式側欄使用。"""
    _require_chat_permission(current_user)
    try:
        conversations = db.query(Conversation).filter(
            Conversation.user_id == current_user.id
        ).order_by(Conversation.last_interacted_at.desc()).all()
    except (ProgrammingError, OperationalError):
        conversations = db.query(Conversation).order_by(Conversation.created_at.desc()).all()

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
                    'timestamp': m.timestamp.isoformat() if m.timestamp else None,
                }
                for m in messages
            ],
            'created_at': conv.created_at.isoformat() if conv.created_at else None,
            'last_interacted_at': conv.last_interacted_at.isoformat() if getattr(conv, 'last_interacted_at', None) else None,
        })
    return {'conversations': result}


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
        # 依 FK 依賴順序刪除：tasks → sessions → llm_turns → event_parts → messages → conversation
        session_ids = [
            r[0] for r in db.query(MultiAgentSession.id)
            .filter(MultiAgentSession.conversation_id == conv.id).all()
        ]
        if session_ids:
            db.query(MultiAgentTask).filter(MultiAgentTask.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(MultiAgentSession).filter(MultiAgentSession.conversation_id == conv.id).delete(synchronize_session=False)
        db.query(LlmTurn).filter(LlmTurn.conversation_id == conv.id).delete(synchronize_session=False)
        db.query(EventPart).filter(EventPart.conversation_id == conv.id).delete(synchronize_session=False)
        db.query(Message).filter(Message.conversation_id == conv.id).delete(synchronize_session=False)
        db.delete(conv)
        db.commit()
    except Exception:
        db.rollback()
        raise validation_error('刪除會話失敗，請稍後再試')

    return {'ok': True, 'conversation_id': str(conversation_id)}
