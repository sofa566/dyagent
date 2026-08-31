from contextlib import asynccontextmanager

from src.api.errors import (
    general_exception_handler,
)
from src.api.routes import agents, auth, chat, logs, mcp, rag, users, admin_dashboard, skill_ui, access_control, cost_usage
from src.api.routes import mcps_admin, skills_admin
from src.api.routes import functions_admin
from src.core.config import settings
from src.core.logging import get_logger
from src.models import *
from src.services.qdrant_service import qdrant_service
from src.services.redis_service import redis_service
from src.services.access_control_service import access_control_service
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = get_logger(__name__)

logger.debug(f"API settings loaded: ")

def _seed_router_agent(db):
    # 目的：確保系統存在主代理（Router）供 /api/chat 統一入口使用。
    # 為什麼：一般使用者不應承擔手動選代理者成本，需先有預設路由代理。
    try:
        from src.models import Agent, Workspace

        router = db.query(Agent).filter(Agent.is_router == True).first()  # noqa: E712
        if router is not None:
            changed = False
            if str(getattr(router, 'agent_class', '') or '') != 'master':
                router.agent_class = 'master'
                changed = True
            if not bool(getattr(router, 'enabled', True)):
                router.enabled = True
                changed = True
            if changed:
                db.commit()
            return

        ws = db.query(Workspace).first()
        if ws is None:
            ws = Workspace(name='default')
            db.add(ws)
            db.commit()
            db.refresh(ws)

        router = Agent(
            name='主代理',
            description='負責將使用者問題分派到合適代理者',
            system_prompt='你是主代理，負責判斷問題並選擇合適的部門代理者。',
            model_type='cloud',
            agent_class='master',
            enabled=True,
            is_router=True,
            model_config={'tier': 'cloud'},
            workspace_id=ws.id,
        )
        db.add(router)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning('router.seed_failed', error=str(e))


def _ensure_weather_skill_open_meteo(db):
    """將既有「天氣查詢」技能切換為 Open-Meteo Python handler。"""
    try:
        from src.models import SkillEntry

        row = db.query(SkillEntry).filter(SkillEntry.name == '天氣查詢').first()
        if row is None:
            row = SkillEntry(
                name='天氣查詢',
                description='使用 Open-Meteo 查詢城市即時天氣',
                enabled=True,
                type='python',
                python_handler='src.skills.open_meteo:get_weather',
                input_schema={
                    'type': 'object',
                    'properties': {
                        'q': {'type': 'string', 'description': '城市名稱，例如 台北'},
                    },
                    'required': ['q'],
                    'additionalProperties': True,
                },
            )
            db.add(row)
            db.commit()
            return

        changed = False
        if str(getattr(row, 'type', '') or '') != 'python':
            row.type = 'python'
            changed = True
        if str(getattr(row, 'python_handler', '') or '') != 'src.skills.open_meteo:get_weather':
            row.python_handler = 'src.skills.open_meteo:get_weather'
            changed = True
        # 轉為 python handler 後，不再依賴 webhook URL
        if getattr(row, 'endpoint_url', None):
            row.endpoint_url = None
            changed = True
        if str(getattr(row, 'http_method', 'POST') or 'POST').upper() != 'POST':
            row.http_method = 'POST'
            changed = True
        if not bool(getattr(row, 'enabled', True)):
            row.enabled = True
            changed = True
        if changed:
            db.commit()
    except Exception as e:
        logger.warning('weather_skill.ensure_failed', error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('application_startup', version=settings.VERSION)
    await redis_service.connect()
    qdrant_service.connect()
    logger.info('database_migrations_expected', mode='alembic_only')

    from sqlalchemy.orm import Session
    from src.core.database import SessionLocal
    from src.schemas.utils import first_user

    db: Session = SessionLocal()
    try:
        message = first_user(db)
        print(message)
        access_control_service.ensure_system_roles(db)
        _ensure_weather_skill_open_meteo(db)
        _seed_router_agent(db)
    finally:
        db.close()

    yield
    logger.info('application_shutdown')
    await redis_service.disconnect()
    qdrant_service.disconnect()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

app.add_exception_handler(Exception, general_exception_handler)

app.include_router(auth.router, prefix='/api', tags=['認證'])
app.include_router(agents.router, prefix='/api', tags=['代理者'])
app.include_router(chat.router, prefix='/api', tags=['聊天'])
app.include_router(mcp.router, prefix='/api', tags=['MCP'])
app.include_router(rag.router, prefix='/api', tags=['RAG'])
app.include_router(users.router, prefix='/api', tags=['使用者'])
app.include_router(access_control.router, prefix='/api', tags=['權限管理'])
app.include_router(logs.router, prefix='/api', tags=['日誌'])
app.include_router(admin_dashboard.router, prefix='/api', tags=['管理儀表板'])
app.include_router(cost_usage.router, prefix='/api', tags=['成本治理'])
app.include_router(mcps_admin.router, prefix='/api', tags=['MCP 管理'])
app.include_router(skills_admin.router, prefix='/api', tags=['技能管理'])
app.include_router(functions_admin.router, prefix='/api', tags=['Functions 管理'])
app.include_router(skill_ui.router, prefix='/api', tags=['技能 UI'])


@app.get('/')
async def root():
    return {'message': 'Welcome to dyagent API', 'version': settings.VERSION}


@app.get('/health')
async def health():
    return {'status': 'healthy'}
