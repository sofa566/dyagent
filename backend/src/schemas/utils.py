from sqlalchemy.orm import Session

from src.models import User
from src.middleware.auth import get_password_hash, verify_password
from src.schemas.user import UserResponse
from src.services.access_control_service import access_control_service


def user_to_response(db: Session, user: User) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        username=user.username,
        email=user.email,
        enabled=bool(getattr(user, 'enabled', True)),
        role=access_control_service.resolve_primary_role_code(db, user),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def first_user(db: Session):
    existing = db.query(User).filter((User.email == 'admin@example.com') | (User.username == 'admin')).first()
    if existing:
        # 開發環境保底：確保預設 admin 帳密可登入
        # 若資料庫已有 admin@example.com 但密碼已遺失，重設為 adminpassword
        try:
            access_control_service.sync_user_system_role_binding(db, existing, 'admin')
            if existing.email == 'admin@example.com' and not verify_password('adminpassword', existing.password_hash):
                existing.password_hash = get_password_hash('adminpassword')
                db.commit()
                return "Admin user password reset to default (adminpassword)."
        except Exception:
            db.rollback()
        return "Admin user already exists."

    # Create the first admin user
    admin_user = User(
        username='admin',
        email='admin@example.com',
        password_hash=get_password_hash('adminpassword'),
    )
    db.add(admin_user)
    db.flush()
    access_control_service.sync_user_system_role_binding(db, admin_user, 'admin')
    db.commit()
    db.refresh(admin_user)

    return f"Admin user created with username: {admin_user.username}, email: {admin_user.email}"
