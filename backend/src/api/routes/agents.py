from fastapi import APIRouter, Depends, Body
import uuid
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import User, Agent, Workspace
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error
from src.services.chat_router import ChatRouter
from src.services.llm_client import LLMClient

router = APIRouter()


@router.get('/agents/public')
async def list_public_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提供一般使用者可見的代理者清單。

    - 權限：僅需 `chat`（一般使用者具備）
    - 欄位：僅回傳基本資訊供前端選擇
    """
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    agents = db.query(Agent).all()
    return {
        'agents': [
            {
                'id': str(a.id),
                'name': a.name,
                'description': a.description,
            }
            for a in agents
        ]
    }


@router.get('/agents/{agent_id}/integrations')
async def get_agent_integrations(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    def _default_mcp(value):
        # 正規化為 list 以簡化前端處理
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            # 若為物件，視為 map，取值為陣列
            return list(value.values())
        return []

    return {
        'mcp_config': _default_mcp(agent.mcp_config or {}),
        'skills': agent.skills or [],
        'rag_config': agent.rag_config or {'enabled': False, 'sources': [], 'topK': 5},
    }


@router.put('/agents/{agent_id}/integrations')
async def update_agent_integrations(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 讀取欄位並做基本驗證
    mcp_cfg = payload.get('mcp_config', []) if isinstance(payload, dict) else []
    skills = payload.get('skills', []) if isinstance(payload, dict) else []
    rag_cfg = payload.get('rag_config', {}) if isinstance(payload, dict) else {}

    if mcp_cfg is not None and not isinstance(mcp_cfg, list):
        raise validation_error('mcp_config 必須為陣列')
    if skills is not None and not isinstance(skills, list):
        raise validation_error('skills 必須為陣列')
    if rag_cfg is not None and not isinstance(rag_cfg, dict):
        raise validation_error('rag_config 必須為物件')

    # 基礎清理
    def _clean_skill(name: str) -> str:
        return (name or '').strip()[:64]

    skills_clean = []
    seen = set()
    for s in (skills or []):
        if not isinstance(s, str):
            continue
        n = _clean_skill(s)
        if n and n not in seen:
            seen.add(n)
            skills_clean.append(n)

    # rag 預設值
    rag_out = {
        'enabled': bool((rag_cfg or {}).get('enabled', False)),
        'sources': list((rag_cfg or {}).get('sources', []) or []),
        'topK': int((rag_cfg or {}).get('topK', 5) or 5),
    }
    if rag_out['topK'] < 1 or rag_out['topK'] > 50:
        raise validation_error('rag_config.topK 必須介於 1..50')

    agent.mcp_config = mcp_cfg or []
    agent.skills = skills_clean
    agent.rag_config = rag_out
    db.commit()
    db.refresh(agent)

    # 簡易審計（以 Log 表）
    try:
        from src.models import Log
        log = Log(
            user_id=current_user.id,
            level='info',
            action='agent.integrations.update',
            resource_type='agent',
            resource_id=agent.id,
            details={'counts': {'mcp': len(agent.mcp_config or []), 'skills': len(agent.skills or []), 'sources': len(rag_out['sources'])}},
            ip_address=None,
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()

    return {
        'mcp_config': agent.mcp_config or [],
        'skills': agent.skills or [],
        'rag_config': agent.rag_config or {'enabled': False, 'sources': [], 'topK': 5},
    }


@router.post('/agents/{agent_id}/mcp-test')
async def test_agent_mcp(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 使用 update_agent 權限作為最低門檻（設定頁測試）
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    conn = (payload or {}).get('connection') if isinstance(payload, dict) else None
    name = (conn or {}).get('name') if isinstance(conn, dict) else None
    base_url = (conn or {}).get('base_url') if isinstance(conn, dict) else None
    if not name or not isinstance(name, str):
        return {'ok': False, 'error': '缺少連線名稱'}
    if not base_url or not isinstance(base_url, str):
        return {'ok': False, 'error': '缺少 base_url'}

    # 最小化測試：僅檢查 URL 格式，不對外連線
    try:
        from urllib.parse import urlparse
        u = urlparse(base_url)
        if not u.scheme or not u.netloc:
            return {'ok': False, 'error': 'base_url 格式不正確'}
    except Exception:
        return {'ok': False, 'error': 'base_url 格式不正確'}

    return {'ok': True, 'error': None, 'details': {}}


@router.post('/agents/{agent_id}/rag-test')
async def test_agent_rag(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    query = (payload or {}).get('query') if isinstance(payload, dict) else ''
    sources = (payload or {}).get('sources') if isinstance(payload, dict) else []
    topk = (payload or {}).get('topK') if isinstance(payload, dict) else 5
    if not isinstance(sources, list):
        return {'ok': False, 'results': [], 'error': 'sources 必須為陣列'}
    try:
        k = int(topk or 5)
        k = 5 if k <= 0 or k > 50 else k
    except Exception:
        k = 5
    # 最小化測試：不呼叫外部向量庫，僅回傳樣本結構
    results = []
    if sources and query:
        results.append({
            'docId': 'sample',
            'score': 0.87,
            'snippet': f'snippet for "{query}" from {sources[0]}'[:120],
        })
    return {'ok': True, 'results': results}


@router.get('/agents')
async def list_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    agents = db.query(Agent).all()
    return {
        'agents': [
            {
                'id': str(a.id),
                'name': a.name,
                'description': a.description,
                'model_type': a.model_type,
                'created_at': a.created_at.isoformat() if a.created_at else None,
                'updated_at': a.updated_at.isoformat() if a.updated_at else None,
            }
            for a in agents
        ]
    }


@router.post('/agents')
async def create_agent(
    name: str,
    description: str = '',
    model_type: str = 'cloud',
    model_config: dict = {},
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'create_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    if not name or len(name.strip()) == 0:
        raise validation_error('Agent name is required')

    workspace = db.query(Workspace).first()
    if not workspace:
        workspace = Workspace(name='Default Workspace')
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

    agent = Agent(
        name=name,
        description=description,
        model_type=model_type,
        model_config=model_config,
        workspace_id=workspace.id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'model_type': agent.model_type,
        'model_config': agent.model_config,
        'created_at': agent.created_at.isoformat() if agent.created_at else None,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.get('/agents/{agent_id}')
async def get_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    # Validate UUID format to avoid DB type conversion errors
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'model_type': agent.model_type,
        'model_config': agent.model_config,
        'mcp_config': agent.mcp_config,
        'skills': agent.skills,
        'tools': agent.tools,
        'rag_config': agent.rag_config,
        'workspace_id': str(agent.workspace_id),
        'created_at': agent.created_at.isoformat() if agent.created_at else None,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.put('/agents/{agent_id}')
async def update_agent(
    agent_id: str,
    name: str | None = None,
    description: str | None = None,
    model_config: dict | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    if name is not None:
        agent.name = name
    if description is not None:
        agent.description = description
    if model_config is not None:
        agent.model_config = model_config

    db.commit()
    db.refresh(agent)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'model_type': agent.model_type,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.delete('/agents/{agent_id}')
async def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'delete_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    db.delete(agent)
    db.commit()

    return None


@router.put('/agents/{agent_id}/llm-config')
async def update_agent_llm_config(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 允許前端傳 raw dict 或 {"model_config": {...}}
    model_config = payload.get('model_config', payload) if isinstance(payload, dict) else None
    if not isinstance(model_config, dict):
        raise validation_error('model_config must be an object')

    agent.model_config = model_config
    db.commit()
    db.refresh(agent)

    return {
        'id': str(agent.id),
        'model_type': agent.model_type,
        'model_config': agent.model_config,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.post('/agents/{agent_id}/llm-test')
async def test_agent_llm(
    agent_id: str,
    overrides: dict | None = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 使用 chat 權限作為測試的最低門檻
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

    # 構建 overrides：以呼叫者提供的覆蓋優先，其次為 agent.model_config
    ov: dict = {}
    if isinstance(agent.model_config, dict):
        ov.update(agent.model_config)
    if isinstance(overrides, dict):
        ov.update(overrides)

    tier = ov.get('tier')
    router = ChatRouter()
    try:
        _ = router.single_turn(
            session_id=str(agent.id),
            agent_id=str(agent.id),
            user_message='ping',
            tier=tier,
            db=None,
            llm_overrides=ov,
        )
        info = getattr(router._llm, 'last_route_info', lambda: {})()  # type: ignore[attr-defined]
        return {
            'ok': True,
            'route': info,
        }
    except Exception as e:
        return {
            'ok': False,
            'error': str(e),
        }


@router.post('/agents/{agent_id}/llm-health')
async def health_agent_llm(
    agent_id: str,
    payload: dict | None = Body(None),
    current_user: User = Depends(get_current_user),
):
    # 使用 chat 權限作為測試的最低門檻
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    overrides = (payload or {}).get('overrides') if isinstance(payload, dict) else None
    mode = (payload or {}).get('mode') if isinstance(payload, dict) else None

    llm = LLMClient()
    llm.init_for_session(session_id=f"health-{agent_id}", preferred_tier=(overrides or {}).get('tier'), overrides=overrides or {})
    try:
        res = llm.health_check(mode=(mode or 'soft'))
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}
