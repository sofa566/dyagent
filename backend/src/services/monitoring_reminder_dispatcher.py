from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from src.core.config import settings
from src.core.database import SessionLocal
from src.core.logging import get_logger
from src.services.monitoring_backfill_service import monitoring_backfill_service
from src.services.monitoring_reminder_service import monitoring_reminder_service
from src.services.redis_service import redis_service

logger = get_logger(__name__)


class MonitoringReminderDispatcher:
    # 目的：以背景排程自動觸發早晚缺報提醒。
    # 為什麼：避免人工每日手動觸發提醒，確保腎友回報流程可持續執行。

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            return
        reminder_enabled = bool(getattr(settings, 'MONITORING_REMINDER_SCHEDULER_ENABLED', False))
        backfill_enabled = bool(getattr(settings, 'MONITORING_BACKFILL_SCHEDULER_ENABLED', True))
        if not reminder_enabled and not backfill_enabled:
            logger.info('monitoring_scheduler_all_disabled')
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info('monitoring_reminder_scheduler_started')

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info('monitoring_reminder_scheduler_stopped')

    async def _run_loop(self) -> None:
        # 目的：週期性檢查是否到達提醒時間窗，並觸發催報派送。
        # 為什麼：將時間判斷與派送執行集中，降低主程式啟動流程複雜度。
        interval_seconds = max(15, int(getattr(settings, 'MONITORING_REMINDER_SCHEDULER_INTERVAL_SECONDS', 60) or 60))
        while True:
            try:
                await self._tick()
            except Exception as error:
                logger.warning('monitoring_reminder_scheduler_tick_failed', error=str(error)[:300])
            await asyncio.sleep(interval_seconds)

    async def _tick(self) -> None:
        # 目的：單次排程循環同時處理提醒派送與歷史補寫。
        # 為什麼：將兩種定時任務集中在同一 dispatcher，方便營運統一控管。
        await self._try_dispatch_reminder()
        await self._try_run_backfill()

    async def _try_dispatch_reminder(self) -> None:
        if not bool(getattr(settings, 'MONITORING_REMINDER_SCHEDULER_ENABLED', False)):
            return
        now = datetime.now()
        window = self._resolve_dispatch_window(now=now)
        if not window:
            return
        if not await self._acquire_dispatch_lock(target_date=now.date().isoformat(), window=window):
            return

        db = SessionLocal()
        try:
            result = await monitoring_reminder_service.dispatch_reminders(
                db=db,
                target_date=now.date(),
                window=window,
                force=False,
            )
            logger.info(
                'monitoring_reminder_scheduler_dispatched',
                date=result.get('date'),
                window=result.get('window'),
                scheduled=result.get('scheduled'),
                sent=result.get('sent'),
                failed=result.get('failed'),
                skipped=result.get('skipped'),
            )
        finally:
            db.close()

    async def _try_run_backfill(self) -> None:
        # 目的：在指定時段自動執行歷史訊息補寫。
        # 為什麼：日常自動補帳可降低人工排查頻率，減少資料缺漏滯留時間。
        if not bool(getattr(settings, 'MONITORING_BACKFILL_SCHEDULER_ENABLED', True)):
            return
        now = datetime.now()
        slot = self._resolve_backfill_slot(now=now)
        if not slot:
            return
        target_date = now.date().isoformat()
        if not await self._acquire_backfill_lock(target_date=target_date, slot=slot):
            return

        lookback_days = max(1, min(int(getattr(settings, 'MONITORING_BACKFILL_LOOKBACK_DAYS', 2) or 2), 14))
        end_date = now.date()
        start_date = end_date - timedelta(days=lookback_days - 1)
        db = SessionLocal()
        try:
            result = await monitoring_backfill_service.run_backfill(
                db=db,
                start_date=start_date,
                end_date=end_date,
                triggered_by=f'scheduler:{slot.lower()}',
                max_scan_rows=5000,
            )
            logger.info(
                'monitoring_backfill_scheduler_completed',
                slot=slot,
                start_date=result.get('date_range', {}).get('start_date'),
                end_date=result.get('date_range', {}).get('end_date'),
                scanned=result.get('scanned_messages'),
                parsed=result.get('parsed_messages'),
                created=result.get('created_records'),
                skipped=result.get('skipped_existing_records'),
                failed=result.get('failed_records'),
            )
        finally:
            db.close()

    def _resolve_dispatch_window(self, *, now: datetime) -> str:
        # 目的：比對目前時間是否命中早/晚提醒時間。
        # 為什麼：只在命中設定時間時派送，避免每輪輪詢都重覆觸發。
        current_hhmm = now.strftime('%H:%M')
        morning_hhmm = self._normalize_hhmm(value=str(getattr(settings, 'MONITORING_REMINDER_MORNING_DISPATCH_TIME', '06:00') or '06:00'))
        evening_hhmm = self._normalize_hhmm(value=str(getattr(settings, 'MONITORING_REMINDER_EVENING_DISPATCH_TIME', '21:00') or '21:00'))
        if current_hhmm == morning_hhmm:
            return 'MORNING'
        if current_hhmm == evening_hhmm:
            return 'EVENING'
        return ''

    def _normalize_hhmm(self, *, value: str) -> str:
        text = str(value or '').strip()
        parts = text.split(':')
        if len(parts) != 2:
            return '00:00'
        try:
            hour = max(0, min(23, int(parts[0])))
            minute = max(0, min(59, int(parts[1])))
        except Exception:
            return '00:00'
        return f'{hour:02d}:{minute:02d}'

    def _resolve_backfill_slot(self, *, now: datetime) -> str:
        # 目的：判斷目前是否命中補寫排程時刻。
        # 為什麼：避免輪詢期間重複執行，僅在設定時間點觸發一次補帳。
        current_hhmm = now.strftime('%H:%M')
        first_run_hhmm = self._normalize_hhmm(value=str(getattr(settings, 'MONITORING_BACKFILL_FIRST_RUN_TIME', '00:10') or '00:10'))
        second_run_hhmm = self._normalize_hhmm(value=str(getattr(settings, 'MONITORING_BACKFILL_SECOND_RUN_TIME', '12:10') or '12:10'))
        if current_hhmm == first_run_hhmm:
            return 'RUN_1'
        if current_hhmm == second_run_hhmm:
            return 'RUN_2'
        return ''

    async def _acquire_dispatch_lock(self, *, target_date: str, window: str) -> bool:
        if redis_service.client is None:
            return True
        lock_key = f'monitoring:dispatch:{target_date}:{window}'
        created = await redis_service.client.set(lock_key, '1', ex=90, nx=True)
        return bool(created)

    async def _acquire_backfill_lock(self, *, target_date: str, slot: str) -> bool:
        if redis_service.client is None:
            return True
        lock_key = f'monitoring:backfill:{target_date}:{slot}'
        created = await redis_service.client.set(lock_key, '1', ex=150, nx=True)
        return bool(created)


monitoring_reminder_dispatcher = MonitoringReminderDispatcher()
