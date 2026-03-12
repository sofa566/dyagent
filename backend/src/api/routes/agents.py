from fastapi import APIRouter, Depends
import uuid
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import User, Agent, Workspace
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error

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
