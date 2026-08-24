from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.middleware.auth import (
    get_current_user,
    get_password_hash,
    create_access_token,
)
from src.models import User
from src.api.errors import validation_error, unauthorized_error
from src.schemas.user import UserCreate, UserLogin, UserResponse, TokenResponse
from src.schemas.utils import user_to_response
from src.services.access_control_service import access_control_service

router = APIRouter()


@router.post('/register', response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(user_data: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(
        (User.email == user_data.email) | (User.username == user_data.username)
    ).first()
    if existing:
        raise validation_error('Email or username already exists')

    normalized_role = str(user_data.role or 'user').strip()
    if normalized_role not in {'admin', 'agent_admin', 'user'}:
        normalized_role = 'user'

    user = User(
        username=user_data.username,
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
    )
    db.add(user)
    db.flush()
    access_control_service.sync_user_system_role_binding(db, user, normalized_role)
    db.commit()
    db.refresh(user)

    return user_to_response(db, user)


@router.post('/login', response_model=TokenResponse)
async def login(credentials: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == credentials.email).first()
    if not user:
        raise unauthorized_error('Invalid email or password')
    if not bool(getattr(user, 'enabled', True)):
        raise unauthorized_error('帳號已停用，請聯絡管理者')

    from src.middleware.auth import verify_password
    if not verify_password(credentials.password, user.password_hash):
        raise unauthorized_error('Invalid email or password')

    access_token = create_access_token(data={'sub': str(user.id)})

    return {
        'token': access_token,
        'user': {
            'id': str(user.id),
            'username': user.username,
            'role': access_control_service.resolve_primary_role_code(db, user),
        },
    }


@router.get('/me', response_model=UserResponse)
async def get_me(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return user_to_response(db, current_user)
