from sqlalchemy.orm import Session
from src.models import User
from src.middleware.auth import get_password_hash, verify_password
from src.schemas.user import UserCreate, UserLogin, UserResponse, TokenResponse

def user_to_response(user: User) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        username=user.username,
        email=user.email,
        role=user.role,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )

def first_user(db: Session):
    existing = db.query(User).filter((User.role == 'admin') | (User.username == 'admin')).first()
    if existing:
        # 開發環境保底：確保預設 admin 帳密可登入
        # 若資料庫已有 admin@example.com 但密碼已遺失，重設為 adminpassword
        try:
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
        role='admin'
    )
    db.add(admin_user)
    db.commit()
    db.refresh(admin_user)

    return f"Admin user created with username: {admin_user.username}, email: {admin_user.email}"
