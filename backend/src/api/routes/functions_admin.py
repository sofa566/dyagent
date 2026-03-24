from fastapi import APIRouter, Depends, Body
from typing import Any
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import FunctionProfile, User
from src.middleware.auth import get_current_user
from src.middleware.rbac import require_role, Role, check_permission
from src.api.errors import not_found_error, validation_error, forbidden_error


router = APIRouter()


def _to_dict(row: FunctionProfile) -> dict[str, Any]:
    template = row.template or ''
    return {
        'id': str(row.id),
        'name': row.name,
        'provider': row.provider or '',
        'function_definition_template': template,
        'template': template,
        'description': row.description or '',
        'enabled': bool(row.enabled),
        'version': int(row.version or 1),
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get('/functions')
@require_role([Role.ADMIN])
async def list_functions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.query(FunctionProfile).order_by(FunctionProfile.created_at.desc()).all()
    return {'functions': [_to_dict(r) for r in rows]}


@router.get('/functions/selectable')
async def list_selectable_functions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        raise forbidden_error()
    rows = db.query(FunctionProfile).filter(FunctionProfile.enabled == True).order_by(FunctionProfile.created_at.desc()).all()  # noqa: E712
    return {'functions': [_to_dict(r) for r in rows]}


@router.post('/functions')
@require_role([Role.ADMIN])
async def create_function(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = str((payload or {}).get('name') or '').strip()
    if not name:
        raise validation_error('name 為必填')
    template_value = (payload or {}).get('function_definition_template')
    if template_value is None:
        template_value = (payload or {}).get('template')
    template = str(template_value or '').strip()
    if not template:
        raise validation_error('function_definition_template 為必填')

    exists = db.query(FunctionProfile).filter(FunctionProfile.name == name).first()
    if exists:
        raise validation_error('名稱已存在')

    row = FunctionProfile(
        name=name,
        provider=str((payload or {}).get('provider') or '').strip() or None,
        template=template,
        description=str((payload or {}).get('description') or ''),
        enabled=bool((payload or {}).get('enabled', True)),
        version=int((payload or {}).get('version') or 1),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.get('/functions/{function_id}')
@require_role([Role.ADMIN])
async def get_function(
    function_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(FunctionProfile).filter(FunctionProfile.id == function_id).first()
    if not row:
        raise not_found_error('Function', function_id)
    return _to_dict(row)


@router.put('/functions/{function_id}')
@require_role([Role.ADMIN])
async def update_function(
    function_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(FunctionProfile).filter(FunctionProfile.id == function_id).first()
    if not row:
        raise not_found_error('Function', function_id)

    if 'name' in (payload or {}):
        name = str((payload or {}).get('name') or '').strip()
        if not name:
            raise validation_error('name 不可為空')
        dup = db.query(FunctionProfile).filter(FunctionProfile.name == name, FunctionProfile.id != row.id).first()
        if dup:
            raise validation_error('名稱已存在')
        row.name = name

    for key in ('provider', 'description', 'enabled', 'version'):
        if key in (payload or {}):
            setattr(row, key, (payload or {}).get(key))

    if 'function_definition_template' in (payload or {}) or 'template' in (payload or {}):
        template_value = (payload or {}).get('function_definition_template')
        if template_value is None:
            template_value = (payload or {}).get('template')
        row.template = str(template_value or '')

    if not str(row.template or '').strip():
        raise validation_error('function_definition_template 不可為空')

    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.delete('/functions/{function_id}')
@require_role([Role.ADMIN])
async def delete_function(
    function_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(FunctionProfile).filter(FunctionProfile.id == function_id).first()
    if not row:
        raise not_found_error('Function', function_id)
    db.delete(row)
    db.commit()
    return {'ok': True, 'id': function_id}
