from sqlalchemy.orm import Session
from src.models import User
from src.middleware.auth import get_password_hash
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