from fastapi import APIRouter, Depends
from src.models import User
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import forbidden_error

router = APIRouter()


@router.get('/mcp/servers')
async def list_mcp_servers(current_user: User = Depends(get_current_user)):
    if not check_permission(current_user, 'read_agent'):
        raise forbidden_error()

    return {'servers': []}


@router.post('/mcp/connect')
async def connect_mcp(
    server_url: str,
    auth_token: str | None = None,
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        raise forbidden_error()

    return {
        'server_id': 'placeholder',
        'status': 'connected',
        'available_tools': [],
    }


@router.get('/mcp/tools')
async def list_mcp_tools(
    server_id: str | None = None,
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent'):
        raise forbidden_error()

    return {'tools': []}
