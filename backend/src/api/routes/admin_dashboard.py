from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import json
import os
import shutil
import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, text, and_
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import require_permission
from src.models import Agent, Conversation, Message, User, LlmTurn, ToolExecutionAudit
from src.models.events import EventPart
from src.services.qdrant_service import qdrant_service
from src.services.redis_service import redis_service

router = APIRouter()


def _serialize_tool_execution_audit(row: ToolExecutionAudit) -> dict[str, object]:
    # 目的：將工具策略稽核 ORM 轉為 API payload。
    # 為什麼：儀表板/營運查詢需要穩定欄位，避免前端自行推斷資料結構。
    try:
        cost_estimate = float(row.cost_estimate) if row.cost_estimate is not None else None
    except Exception:
        cost_estimate = None
    return {
        'id': str(row.id),
        'user_id': str(row.user_id) if row.user_id else None,
        'agent_id': str(row.agent_id) if row.agent_id else None,
        'conversation_id': str(row.conversation_id) if row.conversation_id else None,
        'tool_name': str(row.tool_name or ''),
        'tool_type': str(row.tool_type or ''),
        'risk_level': str(row.risk_level or ''),
        'cost_class': str(row.cost_class or ''),
        'allowlist_passed': bool(row.allowlist_passed),
        'confirmation_required': bool(row.confirmation_required),
        'confirmation_passed': bool(row.confirmation_passed),
        'quota_passed': bool(row.quota_passed),
        'status': str(row.status or ''),
        'deny_reason': str(row.deny_reason or '') or None,
        'payload_keys': list(row.payload_keys or []),
        'cost_estimate': cost_estimate,
        'latency_ms': int(row.latency_ms) if row.latency_ms is not None else None,
        'details': row.details if isinstance(row.details, dict) else {},
        'created_at': row.created_at.isoformat() if row.created_at else None,
    }


def _read_mem_info() -> dict[str, int | float | None]:
    out: dict[str, int | float | None] = {
        "total_bytes": None,
        "available_bytes": None,
        "used_bytes": None,
        "used_percent": None,
    }
    try:
        data = {}
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                parts = v.strip().split()
                if not parts:
                    continue
                data[k.strip()] = int(parts[0]) * 1024

        total = int(data.get("MemTotal", 0) or 0)
        available = int(data.get("MemAvailable", 0) or 0)
        if total > 0:
            used = max(0, total - available)
            out["total_bytes"] = total
            out["available_bytes"] = available
            out["used_bytes"] = used
            out["used_percent"] = round((used / total) * 100.0, 2)
    except Exception:
        pass
    return out


async def _build_overview(db: Session) -> dict:
    now = datetime.now()
    since_15m = now - timedelta(minutes=15)
    since_24h = now - timedelta(hours=24)

    users_total = db.query(func.count(User.id)).scalar() or 0
    agents_total = db.query(func.count(Agent.id)).scalar() or 0
    agents_cloud = db.query(func.count(Agent.id)).filter(Agent.model_type == "cloud").scalar() or 0
    agents_local = db.query(func.count(Agent.id)).filter(Agent.model_type == "local").scalar() or 0

    active_agents_24h = (
        db.query(func.count(func.distinct(Conversation.agent_id)))
        .filter(Conversation.last_interacted_at >= since_24h)
        .scalar()
        or 0
    )

    conversations_24h = (
        db.query(func.count(Conversation.id))
        .filter(Conversation.last_interacted_at >= since_24h)
        .scalar()
        or 0
    )
    messages_24h = db.query(func.count(Message.id)).filter(Message.timestamp >= since_24h).scalar() or 0

    online_users_15m = (
        db.query(func.count(func.distinct(Conversation.user_id)))
        .filter(Conversation.user_id.isnot(None), Conversation.last_interacted_at >= since_15m)
        .scalar()
        or 0
    )

    event_rows = (
        db.query(EventPart)
        .filter(EventPart.created_at >= since_24h, EventPart.type.in_(["text", "tool_result", "tool_error", "react.trace"]))
        .all()
    )

    llm_chars = 0
    llm_cost_usd_24h = 0.0
    react_runs = 0
    react_steps = 0
    tool_success = 0
    tool_error = 0
    for row in event_rows:
        p = row.payload if isinstance(row.payload, dict) else {}
        row_type = str(getattr(row, "type", "") or "")
        if row_type == "text":
            llm_chars += len(str(p.get("delta") or ""))
        elif row_type == "react.trace":
            react_runs += 1
            trace = p.get("trace")
            if isinstance(trace, list):
                react_steps += len(trace)
        elif row_type == "tool_result":
            tool_success += 1
        elif row_type == "tool_error":
            tool_error += 1

    llm_output_tokens_approx = max(0, llm_chars // 4)

    llm_turn_rows = db.query(LlmTurn).filter(LlmTurn.created_at >= since_24h).all()
    agents_map = {
        str(a.id): str(a.name or '')
        for a in db.query(Agent).all()
    }
    agent_cost_token_map: dict[str, dict[str, float | int | str]] = {}
    for turn in llm_turn_rows:
        agent_id = str(getattr(turn, 'agent_id', '') or '')
        if not agent_id:
            continue

        usage = getattr(turn, 'usage', None)
        usage_dict = usage if isinstance(usage, dict) else {}

        input_tokens = int(usage_dict.get('input_tokens') or 0)
        output_tokens = int(usage_dict.get('output_tokens') or 0)
        total_tokens = int(usage_dict.get('total_tokens') or (input_tokens + output_tokens))
        cost_val = getattr(turn, 'cost_usd', None)
        try:
            cost_usd = float(cost_val) if cost_val is not None else 0.0
        except Exception:
            cost_usd = 0.0
        llm_cost_usd_24h += cost_usd

        if agent_id not in agent_cost_token_map:
            agent_cost_token_map[agent_id] = {
                'agent_id': agent_id,
                'agent_name': agents_map.get(agent_id) or f'未知代理({agent_id[:8]})',
                'turns': 0,
                'input_tokens': 0,
                'output_tokens': 0,
                'total_tokens': 0,
                'cost_usd': 0.0,
            }

        bucket = agent_cost_token_map[agent_id]
        bucket['turns'] = int(bucket['turns']) + 1
        bucket['input_tokens'] = int(bucket['input_tokens']) + input_tokens
        bucket['output_tokens'] = int(bucket['output_tokens']) + output_tokens
        bucket['total_tokens'] = int(bucket['total_tokens']) + total_tokens
        bucket['cost_usd'] = float(bucket['cost_usd']) + cost_usd

    assistant_rows = (
        db.query(
            Conversation.agent_id.label('agent_id'),
            func.count(Message.id).label('assistant_turns'),
            func.sum(func.length(Message.content)).label('assistant_chars'),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .filter(
            and_(
                Message.role == 'assistant',
                Message.timestamp >= since_24h,
            )
        )
        .group_by(Conversation.agent_id)
        .all()
    )

    user_rows = (
        db.query(
            Conversation.agent_id.label('agent_id'),
            func.count(Message.id).label('user_turns'),
            func.sum(func.length(Message.content)).label('user_chars'),
        )
        .join(Message, Message.conversation_id == Conversation.id)
        .filter(
            and_(
                Message.role == 'user',
                Message.timestamp >= since_24h,
            )
        )
        .group_by(Conversation.agent_id)
        .all()
    )

    assistant_map: dict[str, tuple[int, int]] = {}
    for row in assistant_rows:
        agent_id = str(getattr(row, 'agent_id', '') or '')
        if not agent_id:
            continue
        assistant_turns = int(getattr(row, 'assistant_turns', 0) or 0)
        assistant_chars = int(getattr(row, 'assistant_chars', 0) or 0)
        assistant_map[agent_id] = (assistant_turns, assistant_chars)

    user_map: dict[str, tuple[int, int]] = {}
    for row in user_rows:
        agent_id = str(getattr(row, 'agent_id', '') or '')
        if not agent_id:
            continue
        user_turns = int(getattr(row, 'user_turns', 0) or 0)
        user_chars = int(getattr(row, 'user_chars', 0) or 0)
        user_map[agent_id] = (user_turns, user_chars)

    merged_breakdown: list[dict[str, float | int | str | bool]] = []
    all_agent_ids = set(list(agent_cost_token_map.keys()) + list(assistant_map.keys()) + list(user_map.keys()))
    for agent_id in all_agent_ids:
        llm_row = agent_cost_token_map.get(agent_id) or {}
        msg_turns, msg_chars = assistant_map.get(agent_id, (0, 0))
        user_turns, user_chars = user_map.get(agent_id, (0, 0))
        msg_input_tokens = max(0, user_chars // 4)
        msg_output_tokens = max(0, msg_chars // 4)

        llm_turns = int(llm_row.get('turns') or 0)
        llm_input_tokens = int(llm_row.get('input_tokens') or 0)
        llm_output_tokens = int(llm_row.get('output_tokens') or 0)
        llm_total_tokens = int(llm_row.get('total_tokens') or (llm_input_tokens + llm_output_tokens))
        llm_cost_usd = float(llm_row.get('cost_usd') or 0.0)

        turns = max(llm_turns, msg_turns, user_turns)
        input_tokens = max(llm_input_tokens, msg_input_tokens)
        output_tokens = max(llm_output_tokens, msg_output_tokens)
        total_tokens = max(llm_total_tokens, input_tokens + output_tokens)
        estimated = (msg_turns > llm_turns) or (user_turns > llm_turns)

        merged_breakdown.append(
            {
                'agent_id': agent_id,
                'agent_name': agents_map.get(agent_id) or f'未知代理({agent_id[:8]})',
                'turns': turns,
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': total_tokens,
                'cost_usd': llm_cost_usd,
                'estimated': estimated,
            }
        )

    agent_llm_breakdown_24h = sorted(
        merged_breakdown,
        key=lambda row: (int(row.get('total_tokens') or 0), float(row.get('cost_usd') or 0.0)),
        reverse=True,
    )

    load1, load5, load15 = (0.0, 0.0, 0.0)
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        pass

    cpu_count = os.cpu_count() or 1
    disk = shutil.disk_usage("/")

    db_ok = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    redis_ok = False
    try:
        if redis_service.client is not None:
            redis_ok = bool(await redis_service.client.ping())
    except Exception:
        redis_ok = False

    qdrant_ok = False
    try:
        if qdrant_service.client is not None:
            qdrant_service.client.get_collections()
            qdrant_ok = True
    except Exception:
        qdrant_ok = False

    return {
        "updated_at": now.isoformat(),
        "services": {
            "database": "ok" if db_ok else "down",
            "redis": "ok" if redis_ok else "down",
            "qdrant": "ok" if qdrant_ok else "down",
        },
        "resources": {
            "cpu": {
                "cores": cpu_count,
                "loadavg": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
                "load_percent_1m": round((load1 / max(cpu_count, 1)) * 100.0, 2),
            },
            "memory": _read_mem_info(),
            "disk": {
                "total_bytes": int(disk.total),
                "used_bytes": int(disk.used),
                "free_bytes": int(disk.free),
                "used_percent": round((disk.used / max(disk.total, 1)) * 100.0, 2),
            },
        },
        "agents": {
            "total": int(agents_total),
            "active_24h": int(active_agents_24h),
            "cloud": int(agents_cloud),
            "local": int(agents_local),
        },
        "users": {
            "total": int(users_total),
            "online_15m": int(online_users_15m),
        },
        "traffic": {
            "conversations_24h": int(conversations_24h),
            "messages_24h": int(messages_24h),
        },
        "llm": {
            "output_chars_24h": int(llm_chars),
            "output_tokens_approx_24h": int(llm_output_tokens_approx),
            "cost_estimate_usd_24h": round(float(llm_cost_usd_24h), 6),
            "turn_rows_24h": int(len(llm_turn_rows)),
            "react_runs_24h": int(react_runs),
            "react_steps_24h": int(react_steps),
            "tool_success_24h": int(tool_success),
            "tool_error_24h": int(tool_error),
            "agent_breakdown_24h": agent_llm_breakdown_24h,
        },
    }


@router.get("/admin/dashboard/overview")
@require_permission('dashboard.read')
async def admin_dashboard_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _build_overview(db)


@router.get("/admin/dashboard/stream")
@require_permission('dashboard.read')
async def admin_dashboard_stream(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    async def _gen():
        while True:
            try:
                payload = await _build_overview(db)
                yield f"data: {json.dumps({'type': 'overview', 'payload': payload}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'heartbeat', 'ts': datetime.now().isoformat()}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                err = {"type": "error", "message": str(e)}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                await asyncio.sleep(2.0)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.get('/admin/tool-execution-audits')
@require_permission('dashboard.read')
async def list_tool_execution_audits(
    tool_name: str | None = Query(default=None),
    status: str | None = Query(default=None),
    risk_level: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：提供工具策略稽核查詢 API。
    # 為什麼：策略層上線後需快速檢視拒絕原因、風險分布與成本訊號。
    query = db.query(ToolExecutionAudit)
    since_at = datetime.now() - timedelta(days=int(days))
    query = query.filter(ToolExecutionAudit.created_at >= since_at)

    normalized_tool_name = str(tool_name or '').strip()
    if normalized_tool_name:
        query = query.filter(ToolExecutionAudit.tool_name == normalized_tool_name)

    normalized_status = str(status or '').strip()
    if normalized_status:
        query = query.filter(ToolExecutionAudit.status == normalized_status)

    normalized_risk_level = str(risk_level or '').strip()
    if normalized_risk_level:
        query = query.filter(ToolExecutionAudit.risk_level == normalized_risk_level)

    normalized_user_id = str(user_id or '').strip()
    if normalized_user_id:
        try:
            user_uuid = uuid.UUID(normalized_user_id)
            query = query.filter(ToolExecutionAudit.user_id == user_uuid)
        except Exception:
            return {
                'items': [],
                'total': 0,
                'limit': int(limit),
                'offset': int(offset),
                'days': int(days),
            }

    total_count = query.count()
    rows = query.order_by(ToolExecutionAudit.created_at.desc()).offset(int(offset)).limit(int(limit)).all()
    return {
        'items': [_serialize_tool_execution_audit(row) for row in rows],
        'total': int(total_count),
        'limit': int(limit),
        'offset': int(offset),
        'days': int(days),
    }
