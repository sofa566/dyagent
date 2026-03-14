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
