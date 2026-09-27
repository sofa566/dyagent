from __future__ import annotations

import asyncio

from src.core.logging import get_logger
from src.services.scheduler_task_service import scheduler_task_service
from src.worker.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(name='src.worker.tasks.trigger_scheduled_task')
def trigger_scheduled_task(task_id: str) -> dict:
    # 目的：由 Celery Beat 觸發排程任務執行。
    # 為什麼：將到點任務轉交 worker，避免 API 進程承擔背景排程負載。
    return asyncio.run(scheduler_task_service.execute_scheduled_task(task_id=str(task_id or '').strip()))


@celery_app.task(name='src.worker.tasks.execute_task_run')
def execute_task_run(run_id: str) -> dict:
    # 目的：執行指定 run log 對應的排程任務。
    # 為什麼：手動 Run Now 需與排程觸發共用同一執行邏輯與稽核流程。
    normalized_run_id = str(run_id or '').strip()
    if not normalized_run_id:
        return {'ok': False, 'error': 'run_id 不可為空'}
    try:
        output_payload = asyncio.run(scheduler_task_service.execute_task_run_by_id(run_id=normalized_run_id))
        return {'ok': True, 'result': output_payload}
    except Exception as error:
        logger.warning('celery_execute_task_run_failed', run_id=normalized_run_id, error=str(error)[:300])
        return {'ok': False, 'error': str(error)[:300]}
