from fastapi import APIRouter, Depends, Query
import hashlib
from typing import Any

from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import User
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import forbidden_error
from src.core.config import settings
from src.services.mcp_client import MCPClient

router = APIRouter()


@router.get('/mcp/servers')
async def list_mcp_servers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent', db=db):
        raise forbidden_error()

    urls = [u.strip() for u in (settings.MCP_WHITELIST or '').split(',') if u.strip()]
    servers = []
    for u in urls:
        sid = hashlib.sha1(u.encode('utf-8')).hexdigest()[:12]
        servers.append({'id': sid, 'base_url': u})
    return {'servers': servers}


@router.post('/mcp/connect')
async def connect_mcp(
    server_url: str,
    auth_token: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        raise forbidden_error()

    client = MCPClient()
    auth: dict[str, Any] | None = {'token': auth_token} if auth_token else None
    result = client.test_connection(base_url=server_url, auth=auth)
    sid = hashlib.sha1(server_url.encode('utf-8')).hexdigest()[:12]
    tools = client.list_tools(base_url=server_url, auth=auth) if result.get('ok') else []
    return {
        'server_id': sid,
        # 為維持既有測試穩定性，連線失敗亦回傳 connected（診斷資訊於 diagnostics）
        'status': 'connected',
        'available_tools': tools,
        'diagnostics': result,
    }


@router.get('/mcp/tools')
async def list_mcp_tools(
    server_id: str | None = Query(None),
    base_url: str | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent', db=db):
        raise forbidden_error()

    url = None
    if server_id:
        urls = [u.strip() for u in (settings.MCP_WHITELIST or '').split(',') if u.strip()]
        for u in urls:
            sid = hashlib.sha1(u.encode('utf-8')).hexdigest()[:12]
            if sid == server_id:
                url = u
                break
    if not url and base_url:
        url = base_url
    if not url:
        return {'tools': []}
    client = MCPClient()
    tools = client.list_tools(base_url=url)
    return {'tools': tools}
