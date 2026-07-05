from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool

from src.core.config import settings

if settings.DATABASE_URL:
    engine = create_engine(
        settings.DATABASE_URL,
        poolclass=NullPool if 'sqlite' in settings.DATABASE_URL else None,
        # echo=settings.DEBUG,
        echo = False,  # 關閉 SQLAlchemy 的 SQL 日誌輸出，改由 structlog 處理
    )
else:
    engine = None

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
