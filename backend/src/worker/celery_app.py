from __future__ import annotations

from celery import Celery

from src.core.config import settings


def _resolve_redis_url(default_value: str) -> str:
    return str(default_value or '').strip() or str(getattr(settings, 'REDIS_URL', 'redis://localhost:6379/0'))


celery_app = Celery(
    'dyagent_scheduler',
    broker=_resolve_redis_url(getattr(settings, 'CELERY_BROKER_URL', '')),
    backend=_resolve_redis_url(getattr(settings, 'CELERY_RESULT_BACKEND', '')),
)

celery_app.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone=str(getattr(settings, 'CELERY_TIMEZONE', 'Asia/Taipei') or 'Asia/Taipei'),
    enable_utc=False,
    task_default_queue=str(getattr(settings, 'CELERY_QUEUE_NAME', 'dyagent.scheduler') or 'dyagent.scheduler'),
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    broker_connection_retry_on_startup=True,
    beat_scheduler='redbeat.RedBeatScheduler',
    redbeat_redis_url=_resolve_redis_url(getattr(settings, 'REDBEAT_REDIS_URL', '')),
    redbeat_key_prefix=str(getattr(settings, 'REDBEAT_KEY_PREFIX', 'redbeat') or 'redbeat'),
)

celery_app.autodiscover_tasks(['src.worker'])
