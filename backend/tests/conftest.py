import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import Base, get_db
from src.api.main import app
from src.models import User, Agent, Workspace
from src.middleware.auth import get_password_hash, create_access_token
from src.services.access_control_service import access_control_service


SQLALCHEMY_DATABASE_URL = 'sqlite:///:memory:'

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={'check_same_thread': False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(scope='function')
def db():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope='function')
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def admin_user(db):
    user = User(
        username='admin',
        email='admin@test.com',
        password_hash=get_password_hash('admin123'),
    )
    db.add(user)
    db.flush()
    access_control_service.sync_user_system_role_binding(db, user, 'admin')
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def regular_user(db):
    user = User(
        username='user',
        email='user@test.com',
        password_hash=get_password_hash('user123'),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def admin_token(admin_user):
    return create_access_token(data={'sub': str(admin_user.id)})


@pytest.fixture
def regular_user_token(regular_user):
    return create_access_token(data={'sub': str(regular_user.id)})


@pytest.fixture
def workspace(db):
    ws = Workspace(name='Default Workspace')
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


@pytest.fixture
def agent(db, workspace):
    agent = Agent(
        name='Test Agent',
        description='Test Description',
        model_type='cloud',
        workspace_id=workspace.id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent
