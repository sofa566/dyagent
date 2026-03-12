from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    PROJECT_NAME: str = 'dyagent'
    VERSION: str = '0.1.0'
    DEBUG: bool = True

    DATABASE_URL: str = 'postgresql://user:password@localhost:5432/dyagent'

    REDIS_URL: str = 'redis://localhost:6379'

    QDRANT_URL: str = 'http://localhost:6333'

    SECRET_KEY: str = 'your-secret-key-change-in-production'
    ALGORITHM: str = 'HS256'
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    OPENAI_API_KEY: str = ''
    ANTHROPIC_API_KEY: str = ''

    class Config:
        env_file = '.env'
        case_sensitive = True


@lru_cache()
def get_settings():
    return Settings()


settings = get_settings()
