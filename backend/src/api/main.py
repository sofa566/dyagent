from contextlib import asynccontextmanager

from src.api.errors import (
    general_exception_handler,
)
from src.api.routes import agents, auth, chat, logs, mcp, rag, users
from src.core.config import settings
from src.core.database import Base, engine
from src.core.logging import get_logger
from src.models import *
from src.services.qdrant_service import qdrant_service
from src.services.redis_service import redis_service
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = get_logger(__name__)


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


@app.get('/')
async def root():
    return {'message': 'Welcome to dyagent API', 'version': settings.VERSION}


@app.get('/health')
async def health():
    return {'status': 'healthy'}
