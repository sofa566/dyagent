from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import json
import os
import shutil

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import Role, require_role
from src.models import Agent, Conversation, Message, User, LlmTurn
from src.models.events import EventPart
from src.services.qdrant_service import qdrant_service
from src.services.redis_service import redis_service

router = APIRouter()


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
    now = datetime.utcnow()
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

    agent_llm_breakdown_24h = sorted(
        list(agent_cost_token_map.values()),
        key=lambda row: (float(row.get('cost_usd') or 0.0), int(row.get('total_tokens') or 0)),
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
            "react_runs_24h": int(react_runs),
            "react_steps_24h": int(react_steps),
            "tool_success_24h": int(tool_success),
            "tool_error_24h": int(tool_error),
            "agent_breakdown_24h": agent_llm_breakdown_24h,
        },
    }


@router.get("/admin/dashboard/overview")
@require_role([Role.ADMIN])
async def admin_dashboard_overview(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await _build_overview(db)


@router.get("/admin/dashboard/stream")
@require_role([Role.ADMIN])
async def admin_dashboard_stream(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    async def _gen():
        while True:
            try:
                payload = await _build_overview(db)
                yield f"data: {json.dumps({'type': 'overview', 'payload': payload}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'heartbeat', 'ts': datetime.utcnow().isoformat()}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                err = {"type": "error", "message": str(e)}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                await asyncio.sleep(2.0)

    return StreamingResponse(_gen(), media_type="text/event-stream")
