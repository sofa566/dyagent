from contextlib import asynccontextmanager

from src.api.errors import (
    general_exception_handler,
)
from src.api.routes import agents, auth, chat, logs, mcp, rag, users, admin_dashboard
from src.api.routes import mcps_admin, skills_admin
from src.core.config import settings
from src.core.database import Base, engine
from src.core.logging import get_logger
from src.models import *
from src.services.qdrant_service import qdrant_service
from src.services.redis_service import redis_service
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = get_logger(__name__)


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
    Base.metadata.create_all(bind=engine)
    logger.info('database_tables_created')

    from sqlalchemy.orm import Session
    from src.core.database import SessionLocal
    from src.schemas.utils import first_user

    db: Session = SessionLocal()
    try:
        message = first_user(db)
        print(message)
        _ensure_weather_skill_open_meteo(db)
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
app.include_router(logs.router, prefix='/api', tags=['日誌'])
app.include_router(admin_dashboard.router, prefix='/api', tags=['管理儀表板'])
app.include_router(mcps_admin.router, prefix='/api', tags=['MCP 管理'])
app.include_router(skills_admin.router, prefix='/api', tags=['技能管理'])


@app.get('/')
async def root():
    return {'message': 'Welcome to dyagent API', 'version': settings.VERSION}


@app.get('/health')
async def health():
    return {'status': 'healthy'}
