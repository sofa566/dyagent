from fastapi import APIRouter, Depends, Body
from typing import Any
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import MCPConnection, User
from src.middleware.auth import get_current_user
from src.middleware.rbac import require_role, Role
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error
from src.services.mcp_client import MCPClient
from src.models import Log


router = APIRouter()


def _discover_schema_from_config(*, client: MCPClient, name: str, transport: str, base_url: str | None = None, auth: dict[str, Any] | None = None, command: str | None = None, args: list[Any] | None = None, env: dict[str, Any] | None = None) -> dict[str, Any]:
    def _builtin_schema_guess() -> dict[str, Any]:
        nm = str(name or '').strip().lower()
        argv = [str(x).strip().lower() for x in (args or []) if isinstance(x, (str, int, float))]
        cmd = str(command or '').strip().lower()
        # 已知常見 server：mcp-server-fetch
        if nm == 'fetch' or 'mcp-server-fetch' in argv or (cmd == 'uvx' and 'fetch' in ' '.join(argv)):
            return {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'title': '網址', 'format': 'uri'},
                    'max_length': {'type': 'integer', 'title': '最大長度', 'minimum': 1, 'default': 5000},
                    'start_index': {'type': 'integer', 'title': '起始位置', 'minimum': 0, 'default': 0},
                },
                'required': ['url'],
            }
        return {}

    transport = (transport or 'remote').strip() or 'remote'
    builtin_schema = _builtin_schema_guess()
    # 已知常見 server 直接快速回填，避免首次 uvx 安裝造成長時間等待
    if builtin_schema and transport == 'stdio':
        return {
            'ok': True,
            'tool_name': name or None,
            'input_schema': builtin_schema,
            'tools': [{'name': name or 'fetch', 'inputSchema': builtin_schema}],
            'selected_by': 'builtin',
        }
    if transport == 'stdio':
        cmd = (command or '').strip() if isinstance(command, str) else ''
        argv = args if isinstance(args, list) else []
        env_map = env if isinstance(env, dict) else {}
        if not cmd:
            return {'ok': False, 'error': 'missing_command', 'tools': [], 'input_schema': {}}
        tools = client.discover_tools_stdio(command=cmd, args=argv, env=env_map)
    else:
        url = (base_url or '').strip() if isinstance(base_url, str) else ''
        auth_map = auth if isinstance(auth, dict) else None
        if not url:
            return {'ok': False, 'error': 'missing_base_url', 'tools': [], 'input_schema': {}}
        tools = client.discover_tools(base_url=url, auth=auth_map)
    picked = client.select_tool_schema(tools=tools, preferred_name=name)
    tool = picked.get('tool') if isinstance(picked, dict) else None
    schema = picked.get('input_schema') if isinstance(picked, dict) else {}
    if not (isinstance(schema, dict) and schema):
        schema = builtin_schema
    return {
        'ok': (bool(tool) or bool(schema)) and isinstance(schema, dict) and bool(schema),
        'tool_name': (tool or {}).get('name') if isinstance(tool, dict) else None,
        'input_schema': schema if isinstance(schema, dict) else {},
        'tools': tools,
        'selected_by': ('preferred_name' if isinstance(tool, dict) and str((tool or {}).get('name') or '').strip() == str(name or '').strip() else ('builtin' if schema and not tool else 'fallback')),
    }


def _to_dict(m: MCPConnection) -> dict[str, Any]:
    return {
        'id': str(m.id),
        'name': m.name,
        'description': m.description or '',
        'enabled': bool(m.enabled),
        'transport': m.transport,
        'base_url': m.base_url or '',
        'auth': m.auth or {},
        'progress_field': m.progress_field or '',
        'eta_field': m.eta_field or '',
        'command': m.command or '',
        'args': m.args or [],
        'env': m.env or {},
        'input_schema': m.input_schema or {},
        'created_at': m.created_at.isoformat() if m.created_at else None,
        'updated_at': m.updated_at.isoformat() if m.updated_at else None,
    }


@router.get('/mcps')
@require_role([Role.ADMIN])
async def list_mcps(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.query(MCPConnection).order_by(MCPConnection.created_at.desc()).all()
    return {'mcps': [_to_dict(r) for r in rows]}


@router.get('/mcps/selectable')
async def list_selectable_mcps(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提供可被代理者綁定的 MCP 清單（admin / agent_admin 可用）。"""
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    rows = db.query(MCPConnection).filter(MCPConnection.enabled == True).order_by(MCPConnection.created_at.desc()).all()  # noqa: E712
    return {'mcps': [_to_dict(r) for r in rows]}


@router.post('/mcps')
@require_role([Role.ADMIN])
async def create_mcp(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = (payload or {}).get('name') or ''
    if not isinstance(name, str) or not name.strip():
        raise validation_error('name 為必填')
    exists = db.query(MCPConnection).filter(MCPConnection.name == name.strip()).first()
    if exists:
        raise validation_error('名稱已存在')
    m = MCPConnection(
        name=name.strip(),
        description=(payload or {}).get('description') or '',
        enabled=bool((payload or {}).get('enabled', True)),
        transport=((payload or {}).get('transport') or 'remote'),
        base_url=(payload or {}).get('base_url') or None,
        auth=(payload or {}).get('auth') or {},
        progress_field=(payload or {}).get('progress_field') or None,
        eta_field=(payload or {}).get('eta_field') or None,
        command=(payload or {}).get('command') or None,
        args=(payload or {}).get('args') or [],
        env=(payload or {}).get('env') or {},
        input_schema=(payload or {}).get('input_schema') or {},
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return _to_dict(m)


@router.get('/mcps/{mcp_id}')
@require_role([Role.ADMIN])
async def get_mcp(
    mcp_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(MCPConnection).filter(MCPConnection.id == mcp_id).first()
    if not row:
        raise not_found_error('MCP', mcp_id)
    return _to_dict(row)


@router.put('/mcps/{mcp_id}')
@require_role([Role.ADMIN])
async def update_mcp(
    mcp_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(MCPConnection).filter(MCPConnection.id == mcp_id).first()
    if not row:
        raise not_found_error('MCP', mcp_id)
    if 'name' in (payload or {}):
        n = (payload or {}).get('name') or ''
        if not isinstance(n, str) or not n.strip():
            raise validation_error('name 不可為空')
        dup = db.query(MCPConnection).filter(MCPConnection.name == n.strip(), MCPConnection.id != row.id).first()
        if dup:
            raise validation_error('名稱已存在')
        row.name = n.strip()
    for k in ('description','enabled','transport','base_url','auth','progress_field','eta_field','command','args','env','input_schema'):
        if k in (payload or {}):
            setattr(row, k, (payload or {}).get(k))
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.post('/mcps/{mcp_id}/test')
@require_role([Role.ADMIN])
async def test_mcp(
    mcp_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(MCPConnection).filter(MCPConnection.id == mcp_id).first()
    if not row:
        raise not_found_error('MCP', mcp_id)
    transport = str(row.transport or 'remote')
    import time as _time
    t0 = _time.time()
    if transport == 'stdio':
        # 目前僅驗證必要欄位存在
        cmd = (row.command or '').strip() if isinstance(row.command, str) else ''
        if not cmd:
            out = {'ok': False, 'error': 'missing_command'}
            try:
                lg = Log(user_id=current_user.id, level='error', action='mcp.test', resource_type='mcp', resource_id=row.id,
                    details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'transport': 'stdio', 'error': 'missing_command'}, ip_address=None)
                db.add(lg); db.commit()
            except Exception:
                db.rollback()
            return out
        out = {'ok': True, 'transport': 'stdio'}
        try:
            lg = Log(user_id=current_user.id, level='info', action='mcp.test', resource_type='mcp', resource_id=row.id,
                details={'ok': True, 'duration_ms': int((_time.time()-t0)*1000), 'transport': 'stdio'}, ip_address=None)
            db.add(lg); db.commit()
        except Exception:
            db.rollback()
        return out
    # remote：呼叫 MCPClient.test_connection
    client = MCPClient()
    try:
        base_url = (row.base_url or '').strip() if isinstance(row.base_url, str) else ''
        if not base_url:
            return {'ok': False, 'error': 'missing_base_url'}
        res = client.test_connection(base_url=base_url, auth=row.auth if isinstance(row.auth, dict) else None)
        try:
            lg = Log(user_id=current_user.id, level='info' if (res or {}).get('ok') else 'error', action='mcp.test', resource_type='mcp', resource_id=row.id,
                details={'ok': bool((res or {}).get('ok')), 'duration_ms': int((_time.time()-t0)*1000), 'transport': 'remote', 'error': (res or {}).get('error')}, ip_address=None)
            db.add(lg); db.commit()
        except Exception:
            db.rollback()
        return res
    except Exception as e:
        out = {'ok': False, 'error': str(e)}
        try:
            lg = Log(user_id=current_user.id, level='error', action='mcp.test', resource_type='mcp', resource_id=row.id,
                details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'transport': str(transport), 'error': str(e)[:200]}, ip_address=None)
            db.add(lg); db.commit()
        except Exception:
            db.rollback()
        return out


@router.get('/mcps/{mcp_id}/tests')
@require_role([Role.ADMIN])
async def list_mcp_tests(
    mcp_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.query(Log).filter(Log.resource_type=='mcp', Log.resource_id==mcp_id, Log.action=='mcp.test').order_by(Log.timestamp.desc()).limit(max(1,min(limit,100))).all()
    def _to(e: Log):
        try:
            return {'id': str(e.id), 'level': e.level, 'details': e.details or {}, 'timestamp': e.timestamp.isoformat() if e.timestamp else None}
        except Exception:
            return {'id': None, 'level': None, 'details': {}, 'timestamp': None}
    return {'items': [_to(x) for x in rows]}


@router.delete('/mcps/{mcp_id}/tests')
@require_role([Role.ADMIN])
async def delete_mcp_tests(
    mcp_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """刪除指定 MCP 的所有測試記錄。"""
    db.query(Log).filter(Log.resource_type == 'mcp', Log.resource_id == mcp_id, Log.action == 'mcp.test').delete(synchronize_session=False)
    db.commit()
    return {'ok': True}


@router.post('/mcps/{mcp_id}/discover-schema')
@require_role([Role.ADMIN])
async def discover_mcp_schema(
    mcp_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(MCPConnection).filter(MCPConnection.id == mcp_id).first()
    if not row:
        raise not_found_error('MCP', mcp_id)
    client = MCPClient()
    try:
        return _discover_schema_from_config(
            client=client,
            name=str(row.name or ''),
            transport=str(row.transport or 'remote'),
            base_url=row.base_url if isinstance(row.base_url, str) else None,
            auth=row.auth if isinstance(row.auth, dict) else None,
            command=row.command if isinstance(row.command, str) else None,
            args=row.args if isinstance(row.args, list) else [],
            env=row.env if isinstance(row.env, dict) else {},
        )
    except Exception as e:
        return {'ok': False, 'error': str(e), 'tools': [], 'input_schema': {}}


@router.post('/mcps/discover-schema-preview')
@require_role([Role.ADMIN])
async def discover_mcp_schema_preview(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    client = MCPClient()
    try:
        return _discover_schema_from_config(
            client=client,
            name=str((payload or {}).get('name') or ''),
            transport=str((payload or {}).get('transport') or 'remote'),
            base_url=(payload or {}).get('base_url') if isinstance((payload or {}).get('base_url'), str) else None,
            auth=(payload or {}).get('auth') if isinstance((payload or {}).get('auth'), dict) else None,
            command=(payload or {}).get('command') if isinstance((payload or {}).get('command'), str) else None,
            args=(payload or {}).get('args') if isinstance((payload or {}).get('args'), list) else [],
            env=(payload or {}).get('env') if isinstance((payload or {}).get('env'), dict) else {},
        )
    except Exception as e:
        return {'ok': False, 'error': str(e), 'tools': [], 'input_schema': {}}


@router.delete('/mcps/{mcp_id}')
@require_role([Role.ADMIN])
async def delete_mcp(
    mcp_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(MCPConnection).filter(MCPConnection.id == mcp_id).first()
    if not row:
        raise not_found_error('MCP', mcp_id)
    db.delete(row)
    db.commit()
    return {'ok': True}


@router.post('/mcps/import-mcpservers')
@require_role([Role.ADMIN])
async def import_mcpservers(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """將 Anthropic/Claude Desktop 的 mcpServers 設定匯入為本地 stdio 連線。

    輸入格式示例：
    {
      "mcpServers": {
        "weather": { "command": "python", "args": ["-m", "mcp_weather_server"], "disabled": false }
      }
    }
    """
    root = payload or {}
    servers = root.get('mcpServers') if isinstance(root, dict) else None
    if not isinstance(servers, dict):
      raise validation_error('payload.mcpServers 必須為物件')
    created = 0
    updated = 0
    from src.models import MCPConnection
    for name, spec in servers.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            continue
        cmd = (spec.get('command') or '').strip()
        args = spec.get('args') if isinstance(spec.get('args'), list) else []
        enabled = not bool(spec.get('disabled', False))
        if not cmd:
            continue
        row = db.query(MCPConnection).filter(MCPConnection.name == name.strip()).first()
        if row is None:
            row = MCPConnection(
                name=name.strip(),
                description='imported from mcpServers',
                enabled=enabled,
                transport='stdio',
                command=cmd,
                args=args,
                env={},
            )
            db.add(row)
            created += 1
        else:
            row.enabled = enabled
            row.transport = 'stdio'
            row.command = cmd
            row.args = args
            updated += 1
    db.commit()
    return { 'ok': True, 'created': created, 'updated': updated }
