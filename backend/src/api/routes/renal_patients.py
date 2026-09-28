from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error, not_found_error, validation_error
from src.core.database import get_db
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import RenalPatient, User

router = APIRouter()


def _has_any_permission(*, current_user: User, db: Session, permission_keys: list[str]) -> bool:
    return any(check_permission(current_user, permission_key, db=db) for permission_key in permission_keys)


def _require_renal_permission(*, current_user: User, db: Session, permission_keys: list[str]) -> None:
    can_access = _has_any_permission(current_user=current_user, db=db, permission_keys=permission_keys)
    if not can_access:
        raise forbidden_error('無權限管理腎友主檔')


@router.get('/renals')
async def list_renals(
    search_field: str = Query(default='name'),
    keyword: str = Query(default=''),
    include_disabled: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_permission(
        current_user=current_user,
        db=db,
        permission_keys=['renal.read', 'read_logs', 'update_agent'],
    )
    normalized_field = str(search_field or 'name').strip().lower()
    normalized_keyword = str(keyword or '').strip()
    if normalized_field not in {'name', 'tel_no', 'id_card'}:
        raise validation_error('search_field 僅支援 name/tel_no/id_card')

    query = db.query(RenalPatient)
    if not include_disabled:
        query = query.filter(RenalPatient.enabled == True)  # noqa: E712
    if normalized_keyword:
        like_keyword = f'%{normalized_keyword}%'
        if normalized_field == 'name':
            query = query.filter(RenalPatient.display_name.like(like_keyword))
        elif normalized_field == 'tel_no':
            query = query.filter(RenalPatient.tel_no.like(like_keyword))
        else:
            query = query.filter(RenalPatient.id_card.like(like_keyword))

    rows = query.order_by(RenalPatient.patient_code.asc()).all()
    return {
        'ok': True,
        'items': [
            {
                'id': str(row.patient_code),
                'name': str(row.display_name or ''),
                'tel_no': str(row.tel_no or ''),
                'id_card': str(row.id_card or ''),
                'med_history': str(row.med_history or ''),
                'diagnosis': str(row.diagnosis or ''),
                'primary_nurse_id': str(row.primary_nurse_id or ''),
                'line_user_id': str(row.line_user_id or ''),
                'enabled': bool(row.enabled),
            }
            for row in rows
        ],
    }


@router.post('/renals')
async def create_renal(
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_permission(
        current_user=current_user,
        db=db,
        permission_keys=['renal.create', 'read_logs', 'update_agent'],
    )
    patient_code = str((payload or {}).get('id') or '').strip().upper()
    display_name = str((payload or {}).get('name') or '').strip()
    if not patient_code:
        raise validation_error('id 不可為空')
    if not display_name:
        raise validation_error('name 不可為空')

    existing = db.query(RenalPatient).filter(RenalPatient.patient_code == patient_code).first()
    if existing is not None:
        raise validation_error('id 已存在，請使用其他值')

    row = RenalPatient(
        patient_code=patient_code,
        display_name=display_name,
        tel_no=str((payload or {}).get('tel_no') or '').strip() or None,
        id_card=str((payload or {}).get('id_card') or '').strip() or None,
        med_history=str((payload or {}).get('med_history') or '').strip() or None,
        diagnosis=str((payload or {}).get('diagnosis') or '').strip() or None,
        primary_nurse_id=str((payload or {}).get('primary_nurse_id') or '').strip() or None,
        line_user_id=str((payload or {}).get('line_user_id') or '').strip() or None,
        enabled=bool((payload or {}).get('enabled', True)),
    )
    db.add(row)
    db.commit()
    return {'ok': True, 'id': patient_code, 'created': True}


@router.put('/renals/{patient_id}')
async def update_renal(
    patient_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_permission(
        current_user=current_user,
        db=db,
        permission_keys=['renal.update', 'read_logs', 'update_agent'],
    )
    normalized_id = str(patient_id or '').strip().upper()
    row = db.query(RenalPatient).filter(RenalPatient.patient_code == normalized_id).first()
    if row is None:
        raise not_found_error('RenalPatient', normalized_id)

    name_value = str((payload or {}).get('name') or row.display_name or '').strip()
    if not name_value:
        raise validation_error('name 不可為空')
    row.display_name = name_value
    if 'tel_no' in (payload or {}):
        row.tel_no = str((payload or {}).get('tel_no') or '').strip() or None
    if 'id_card' in (payload or {}):
        row.id_card = str((payload or {}).get('id_card') or '').strip() or None
    if 'med_history' in (payload or {}):
        row.med_history = str((payload or {}).get('med_history') or '').strip() or None
    if 'diagnosis' in (payload or {}):
        row.diagnosis = str((payload or {}).get('diagnosis') or '').strip() or None
    if 'primary_nurse_id' in (payload or {}):
        row.primary_nurse_id = str((payload or {}).get('primary_nurse_id') or '').strip() or None
    if 'line_user_id' in (payload or {}):
        row.line_user_id = str((payload or {}).get('line_user_id') or '').strip() or None
    if 'enabled' in (payload or {}):
        row.enabled = bool((payload or {}).get('enabled'))
    db.add(row)
    db.commit()
    return {'ok': True, 'id': normalized_id, 'updated': True}


@router.delete('/renals/{patient_id}')
async def delete_renal(
    patient_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_renal_permission(
        current_user=current_user,
        db=db,
        permission_keys=['renal.delete', 'read_logs', 'update_agent'],
    )
    normalized_id = str(patient_id or '').strip().upper()
    row = db.query(RenalPatient).filter(RenalPatient.patient_code == normalized_id).first()
    if row is None:
        raise not_found_error('RenalPatient', normalized_id)

    row.enabled = False
    db.add(row)
    db.commit()
    return {'ok': True, 'id': normalized_id, 'deleted': True}
