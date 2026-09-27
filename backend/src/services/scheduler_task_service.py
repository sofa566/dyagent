from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy.orm import Session

from src.api.errors import not_found_error, validation_error
from src.core.config import settings
from src.core.database import SessionLocal
from src.core.logging import get_logger
from src.models import User
from src.models.renal_care import ScheduledTask, ScheduledTaskRun, ScheduledTaskTemplate
from src.services.monitoring_backfill_service import monitoring_backfill_service
from src.services.monitoring_reminder_service import monitoring_reminder_service

logger = get_logger(__name__)

DEFAULT_TASK_SOURCE_SYSTEM = 'system'
DEFAULT_SCRIPT_TIMEOUT_SECONDS = 120
MAX_SCRIPT_TIMEOUT_SECONDS = 3600
SCRIPT_OUTPUT_PREVIEW_MAX_CHARS = 1000
DEFAULT_HEALTH_EDUCATION_ROTATE_EMPTY_ALERT_THRESHOLD = 3
MAX_HEALTH_EDUCATION_ROTATE_EMPTY_ALERT_THRESHOLD = 20
ALLOWED_EXECUTOR_TYPES = {
    'http_call',
    'bash_script',
    'nodejs_script',
    'python_script',
    'renal_reminder_dispatch',
    'monitoring_backfill',
    'health_education_dispatch',
}
CRON_PATTERN = re.compile(r'^[\d\*/,\-]+$')
UNSAFE_BASH_TOKENS = (';', '&&', '||', '|', '>', '<', '`', '$(')


class SchedulerTaskService:
    # 目的：集中管理 Crontab 任務定義、模板與執行。
    # 為什麼：讓排程規則可由後台維護，同時保留可稽核的執行紀錄。

    def list_tasks(self, *, db: Session, only_enabled: bool = False) -> dict:
        # 目的：查詢排程任務清單。
        # 為什麼：列表以建立時間倒序可保持穩定，不會因編輯/啟停而頻繁換位。
        query = db.query(ScheduledTask)
        if only_enabled:
            query = query.filter(ScheduledTask.enabled == True)  # noqa: E712
        rows = query.order_by(ScheduledTask.created_at.desc()).all()
        return {'ok': True, 'items': [self._serialize_task(task=row) for row in rows]}

    def create_task(self, *, db: Session, payload: dict, current_user: User | None) -> dict:
        normalized = self._normalize_task_payload(db=db, payload=payload)
        row = ScheduledTask(
            name=normalized['name'],
            description=normalized['description'],
            cron_expression=normalized['cron_expression'],
            timezone=normalized['timezone'],
            task_type=normalized['task_type'],
            template_id=normalized.get('template_id'),
            payload=normalized['payload'],
            enabled=normalized['enabled'],
            source='manual',
            created_by_user_id=str(getattr(current_user, 'id', '') or '') or None,
            updated_by_user_id=str(getattr(current_user, 'id', '') or '') or None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        self._sync_schedule_entry(task=row)
        return {'ok': True, 'item': self._serialize_task(task=row)}

    def update_task(self, *, db: Session, task_id: str, payload: dict, current_user: User | None) -> dict:
        row = self._get_task_or_error(db=db, task_id=task_id)
        normalized = self._normalize_task_payload(db=db, payload=payload, partial=True)
        for key, value in normalized.items():
            setattr(row, key, value)
        row.updated_by_user_id = str(getattr(current_user, 'id', '') or '') or None
        db.add(row)
        db.commit()
        db.refresh(row)
        self._sync_schedule_entry(task=row)
        return {'ok': True, 'item': self._serialize_task(task=row)}

    def delete_task(self, *, db: Session, task_id: str) -> dict:
        row = self._get_task_or_error(db=db, task_id=task_id)
        task_key = self._build_redbeat_key(task_id=str(row.id))
        db.query(ScheduledTaskRun).filter(ScheduledTaskRun.task_id == row.id).delete(synchronize_session=False)
        db.delete(row)
        db.commit()
        self._remove_schedule_entry(task_key=task_key)
        return {'ok': True}

    async def run_task_now(self, *, db: Session, task_id: str, current_user: User | None) -> dict:
        row = self._get_task_or_error(db=db, task_id=task_id)
        run_row = self._create_run_row(
            db=db,
            task_id=str(row.id),
            trigger_source='manual',
            input_payload=row.payload if isinstance(row.payload, dict) else {},
            executed_by_user_id=str(getattr(current_user, 'id', '') or '') or None,
        )
        dispatched = self._dispatch_run_to_celery(task_id=str(row.id), run_id=str(run_row.id))
        if not dispatched:
            await self._execute_task(db=db, task=row, run_row=run_row)
        db.refresh(run_row)
        return {'ok': True, 'queued': bool(dispatched), 'task': self._serialize_task(task=row), 'run': self._serialize_run(run_row=run_row)}

    def list_runs(self, *, db: Session, task_id: str, limit: int) -> dict:
        # 目的：查詢任務最近執行紀錄並回傳告警摘要。
        # 為什麼：營運需要從 run 紀錄快速看出排程是否持續「無可發文章」，避免長期空轉未發現。
        row = self._get_task_or_error(db=db, task_id=task_id)
        safe_limit = max(1, min(int(limit or 20), 200))
        run_rows = (
            db.query(ScheduledTaskRun)
            .filter(ScheduledTaskRun.task_id == row.id)
            .order_by(ScheduledTaskRun.created_at.desc())
            .limit(safe_limit)
            .all()
        )
        alert_items = self._build_task_alerts(task=row, run_rows=run_rows)
        return {
            'ok': True,
            'task': self._serialize_task(task=row),
            'runs': [self._serialize_run(run_row=item) for item in run_rows],
            'alerts': alert_items,
        }

    def list_templates(self, *, db: Session, enabled_only: bool = False) -> dict:
        query = db.query(ScheduledTaskTemplate)
        if enabled_only:
            query = query.filter(ScheduledTaskTemplate.enabled == True)  # noqa: E712
        rows = query.order_by(ScheduledTaskTemplate.updated_at.desc()).all()
        return {'ok': True, 'items': [self._serialize_template(row=row) for row in rows]}

    def create_template(self, *, db: Session, payload: dict) -> dict:
        normalized = self._normalize_template_payload(payload=payload)
        exists = db.query(ScheduledTaskTemplate).filter(ScheduledTaskTemplate.template_key == normalized['template_key']).first()
        if exists is not None:
            raise validation_error('template_key 已存在')
        row = ScheduledTaskTemplate(
            template_key=normalized['template_key'],
            name=normalized['name'],
            description=normalized['description'],
            executor_type=normalized['executor_type'],
            payload_schema=normalized['payload_schema'],
            default_payload=normalized['default_payload'],
            enabled=normalized['enabled'],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'item': self._serialize_template(row=row)}

    def update_template(self, *, db: Session, template_id: str, payload: dict) -> dict:
        row = self._get_template_or_error(db=db, template_id=template_id)
        normalized = self._normalize_template_payload(payload=payload, partial=True)
        if 'template_key' in normalized:
            exists = (
                db.query(ScheduledTaskTemplate)
                .filter(ScheduledTaskTemplate.template_key == normalized['template_key'], ScheduledTaskTemplate.id != row.id)
                .first()
            )
            if exists is not None:
                raise validation_error('template_key 已存在')
        for key, value in normalized.items():
            setattr(row, key, value)
        db.add(row)
        db.commit()
        db.refresh(row)
        return {'ok': True, 'item': self._serialize_template(row=row)}

    def delete_template(self, *, db: Session, template_id: str) -> dict:
        row = self._get_template_or_error(db=db, template_id=template_id)
        using_count = db.query(ScheduledTask).filter(ScheduledTask.template_id == row.id).count()
        if using_count > 0:
            raise validation_error('此模板仍被任務使用，請先解除或刪除任務')
        db.delete(row)
        db.commit()
        return {'ok': True}

    async def execute_scheduled_task(self, *, task_id: str) -> dict:
        db = SessionLocal()
        try:
            task_row = self._get_task_or_error(db=db, task_id=task_id)
            run_row = self._create_run_row(db=db, task_id=str(task_row.id), trigger_source='scheduler', input_payload=task_row.payload or {}, executed_by_user_id=None)
            output_payload = await self._execute_task(db=db, task=task_row, run_row=run_row)
            return {'ok': True, 'run_id': str(run_row.id), 'result': output_payload}
        finally:
            db.close()

    async def execute_task_run_by_id(self, *, run_id: str) -> dict:
        db = SessionLocal()
        try:
            run_row = db.query(ScheduledTaskRun).filter(ScheduledTaskRun.id == run_id).first()
            if run_row is None:
                raise not_found_error('ScheduledTaskRun', run_id)
            task_row = self._get_task_or_error(db=db, task_id=str(run_row.task_id))
            return await self._execute_task(db=db, task=task_row, run_row=run_row)
        finally:
            db.close()

    def ensure_default_renal_reminder_tasks(self, *, db: Session) -> None:
        try:
            self._ensure_default_templates(db=db)
            reminder_enabled = bool(getattr(settings, 'MONITORING_REMINDER_SCHEDULER_ENABLED', False))
            morning_hhmm = str(getattr(settings, 'MONITORING_REMINDER_MORNING_DISPATCH_TIME', '06:00') or '06:00').strip()
            evening_hhmm = str(getattr(settings, 'MONITORING_REMINDER_EVENING_DISPATCH_TIME', '21:00') or '21:00').strip()
            backfill_enabled = bool(getattr(settings, 'MONITORING_BACKFILL_SCHEDULER_ENABLED', True))
            backfill_first_hhmm = str(getattr(settings, 'MONITORING_BACKFILL_FIRST_RUN_TIME', '00:10') or '00:10').strip()
            backfill_second_hhmm = str(getattr(settings, 'MONITORING_BACKFILL_SECOND_RUN_TIME', '12:10') or '12:10').strip()
            lookback_days = max(1, min(int(getattr(settings, 'MONITORING_BACKFILL_LOOKBACK_DAYS', 2) or 2), 14))
            defaults = [
                {'name': 'LINE-早晨提醒', 'description': '系統任務：每日早晨提醒腎友回報', 'cron_expression': self._hhmm_to_cron(morning_hhmm), 'timezone': 'Asia/Taipei', 'task_type': 'renal_reminder_dispatch', 'payload': {'window': 'MORNING', 'force': False}, 'enabled': reminder_enabled, 'template_key': 'line.morning.reminder'},
                {'name': 'LINE-晚間提醒', 'description': '系統任務：每日晚間提醒腎友回報', 'cron_expression': self._hhmm_to_cron(evening_hhmm), 'timezone': 'Asia/Taipei', 'task_type': 'renal_reminder_dispatch', 'payload': {'window': 'EVENING', 'force': False}, 'enabled': reminder_enabled, 'template_key': 'line.evening.reminder'},
                {'name': 'LINE-凌晨補寫', 'description': '系統任務：凌晨補寫最近資料', 'cron_expression': self._hhmm_to_cron(backfill_first_hhmm), 'timezone': 'Asia/Taipei', 'task_type': 'monitoring_backfill', 'payload': {'lookback_days': lookback_days}, 'enabled': backfill_enabled, 'template_key': 'line.monitoring.backfill'},
                {'name': 'LINE-中午補寫', 'description': '系統任務：中午補寫最近資料', 'cron_expression': self._hhmm_to_cron(backfill_second_hhmm), 'timezone': 'Asia/Taipei', 'task_type': 'monitoring_backfill', 'payload': {'lookback_days': lookback_days}, 'enabled': backfill_enabled, 'template_key': 'line.monitoring.backfill'},
            ]
            for default_task in defaults:
                self._upsert_system_task(db=db, payload=default_task)
            db.commit()
            self.sync_all_enabled_tasks_to_schedule(db=db)
        except Exception as error:
            db.rollback()
            logger.warning('scheduler_default_tasks_seed_skipped', error=str(error)[:300])

    def sync_all_enabled_tasks_to_schedule(self, *, db: Session) -> None:
        rows = db.query(ScheduledTask).all()
        for row in rows:
            self._sync_schedule_entry(task=row)

    def _ensure_default_templates(self, *, db: Session) -> None:
        defaults = [
            {'template_key': 'line.morning.reminder', 'name': 'LINE 早晨提醒', 'description': '提醒腎友回報早晨血壓與體重', 'executor_type': 'renal_reminder_dispatch', 'payload_schema': {'required': ['window']}, 'default_payload': {'window': 'MORNING', 'force': False}, 'enabled': True},
            {'template_key': 'line.evening.reminder', 'name': 'LINE 晚間提醒', 'description': '提醒腎友回報晚間血壓與體重', 'executor_type': 'renal_reminder_dispatch', 'payload_schema': {'required': ['window']}, 'default_payload': {'window': 'EVENING', 'force': False}, 'enabled': True},
            {'template_key': 'line.monitoring.backfill', 'name': 'LINE 監測補寫', 'description': '回補最近監測紀錄', 'executor_type': 'monitoring_backfill', 'payload_schema': {'required': ['lookback_days']}, 'default_payload': {'lookback_days': 2}, 'enabled': True},
        ]
        for payload in defaults:
            row = db.query(ScheduledTaskTemplate).filter(ScheduledTaskTemplate.template_key == payload['template_key']).first()
            if row is None:
                db.add(ScheduledTaskTemplate(**payload))
                continue
            row.name = payload['name']
            row.description = payload['description']
            row.executor_type = payload['executor_type']
            row.payload_schema = payload['payload_schema']
            row.default_payload = payload['default_payload']
            row.enabled = payload['enabled']
            db.add(row)

    def _upsert_system_task(self, *, db: Session, payload: dict) -> None:
        row = db.query(ScheduledTask).filter(ScheduledTask.name == payload['name']).first()
        template_row = None
        if payload.get('template_key'):
            template_row = db.query(ScheduledTaskTemplate).filter(ScheduledTaskTemplate.template_key == payload['template_key']).first()
        if row is None:
            row = ScheduledTask(
                name=payload['name'],
                description=payload['description'],
                cron_expression=payload['cron_expression'],
                timezone=payload['timezone'],
                task_type=payload['task_type'],
                template_id=str(template_row.id) if template_row else None,
                payload=payload['payload'],
                enabled=payload['enabled'],
                source=DEFAULT_TASK_SOURCE_SYSTEM,
            )
            db.add(row)
            return
        if str(row.source or '') != DEFAULT_TASK_SOURCE_SYSTEM:
            return
        row.description = payload['description']
        row.cron_expression = payload['cron_expression']
        row.timezone = payload['timezone']
        row.task_type = payload['task_type']
        row.template_id = str(template_row.id) if template_row else None
        row.payload = payload['payload']
        row.enabled = payload['enabled']
        db.add(row)

    async def _execute_task(self, *, db: Session, task: ScheduledTask, run_row: ScheduledTaskRun) -> dict:
        start_at = datetime.now()
        run_row.started_at = start_at
        run_row.status = 'running'
        db.add(run_row)
        db.commit()
        output_payload: dict = {}
        try:
            executor_type = str(task.task_type)
            if executor_type == 'renal_reminder_dispatch':
                output_payload = await self._execute_renal_reminder_dispatch(db=db, task=task)
            elif executor_type == 'monitoring_backfill':
                output_payload = await self._execute_monitoring_backfill(db=db, task=task)
            elif executor_type == 'http_call':
                output_payload = await self._execute_http_call(task=task)
            elif executor_type == 'bash_script':
                output_payload = await self._execute_script_task(task=task, runtime='bash')
            elif executor_type == 'nodejs_script':
                output_payload = await self._execute_script_task(task=task, runtime='node')
            elif executor_type == 'python_script':
                output_payload = await self._execute_script_task(task=task, runtime='python')
            elif executor_type == 'health_education_dispatch':
                output_payload = await self._execute_health_education_dispatch(db=db, task=task)
            else:
                raise validation_error('不支援的 executor_type')
            run_row.status = 'success'
            run_row.output_payload = output_payload
            run_row.error_message = None
        except Exception as error:
            run_row.status = 'failed'
            run_row.error_message = str(error)[:500]
            run_row.output_payload = {}
            output_payload = {'error': str(error)[:500]}
            logger.warning('scheduled_task_run_failed', task_id=str(task.id), error=str(error)[:300])
        finish_at = datetime.now()
        run_row.finished_at = finish_at
        run_row.duration_ms = int((finish_at - start_at).total_seconds() * 1000)
        db.add(run_row)
        db.commit()
        db.refresh(run_row)
        return output_payload

    async def _execute_renal_reminder_dispatch(self, *, db: Session, task: ScheduledTask) -> dict:
        payload = task.payload if isinstance(task.payload, dict) else {}
        window = str(payload.get('window') or 'EVENING').strip().upper()
        force = bool(payload.get('force'))
        return await monitoring_reminder_service.dispatch_reminders(db=db, target_date=datetime.now().date(), window=window, force=force)

    async def _execute_monitoring_backfill(self, *, db: Session, task: ScheduledTask) -> dict:
        payload = task.payload if isinstance(task.payload, dict) else {}
        lookback_days = max(1, min(int(payload.get('lookback_days') or 2), 14))
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=lookback_days - 1)
        return await monitoring_backfill_service.run_backfill(db=db, start_date=start_date, end_date=end_date, triggered_by='scheduler', max_scan_rows=int(payload.get('max_scan_rows') or 5000))

    async def _execute_http_call(self, *, task: ScheduledTask) -> dict:
        payload = task.payload if isinstance(task.payload, dict) else {}
        method = str(payload.get('method') or 'POST').strip().upper()
        url = str(payload.get('url') or '').strip()
        timeout_seconds = max(2, min(int(payload.get('timeout_seconds') or 15), 120))
        headers = payload.get('headers') if isinstance(payload.get('headers'), dict) else {}
        json_payload = payload.get('json') if isinstance(payload.get('json'), dict) else {}
        if not url:
            raise validation_error('http_call 任務缺少 url')
        allowed_hosts = [item.strip() for item in str(payload.get('allow_hosts') or '').split(',') if item.strip()]
        if allowed_hosts and not any(url.startswith(host) for host in allowed_hosts):
            raise validation_error('http_call 目標 url 不在 allow_hosts 清單')
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.request(method=method, url=url, headers=headers, json=json_payload)
        if response.status_code >= 400:
            raise validation_error(f'http_call 失敗: {response.status_code} {response.text[:200]}')
        return {'status_code': response.status_code, 'response_preview': str(response.text or '')[:500]}

    async def _execute_health_education_dispatch(self, *, db: Session, task: ScheduledTask) -> dict:
        # 目的：執行衛教排程發送（固定單篇或自動輪替）。
        # 為什麼：營運需要同一支排程同時支援指定文章與每次自動換文，降低重複推播風險。
        payload = task.payload if isinstance(task.payload, dict) else {}
        content_id = str(payload.get('content_id') or '').strip()
        dispatch_mode = str(payload.get('dispatch_mode') or '').strip().lower()
        audience_rule = str(payload.get('audience_rule') or 'all').strip().lower() or 'all'
        try:
            cooldown_days = int(payload.get('cooldown_days') or 30)
        except Exception as error:
            raise validation_error('cooldown_days 需為數字') from error
        from src.services.health_education_service import health_education_service

        if dispatch_mode not in {'fixed', 'rotate'}:
            dispatch_mode = 'fixed' if content_id else 'rotate'
        if dispatch_mode == 'rotate':
            return await health_education_service.send_next_content_now(
                db=db,
                audience_rule=audience_rule,
                trigger_source='scheduler',
                cooldown_days=cooldown_days,
            )
        if not content_id:
            raise validation_error('health_education_dispatch 任務缺少 content_id')

        return await health_education_service.send_content_now(
            db=db,
            content_id=content_id,
            audience_rule=audience_rule,
            trigger_source='scheduler',
        )

    async def _execute_script_task(self, *, task: ScheduledTask, runtime: str) -> dict:
        # 目的：執行本機腳本任務（bash/node/python）。
        # 為什麼：部分排程需要離線處理、資料整理或系統腳本，不適合只用 http_call。
        payload = task.payload if isinstance(task.payload, dict) else {}
        timeout_seconds = max(2, min(int(payload.get('timeout_seconds') or DEFAULT_SCRIPT_TIMEOUT_SECONDS), MAX_SCRIPT_TIMEOUT_SECONDS))
        workdir_path = self._resolve_workdir(payload=payload)

        if runtime == 'bash':
            command_text = str(payload.get('command') or '').strip()
            if not command_text:
                raise validation_error('bash_script 任務缺少 command')
            self._validate_bash_command(command_text=command_text)
            process = await asyncio.create_subprocess_exec(
                '/bin/bash',
                '-lc',
                command_text,
                cwd=str(workdir_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            file_path = self._resolve_script_file_path(payload=payload)
            args = payload.get('args') if isinstance(payload.get('args'), list) else []
            args = [str(item) for item in args]
            executable = 'node' if runtime == 'node' else 'python'
            process = await asyncio.create_subprocess_exec(
                executable,
                str(file_path),
                *args,
                cwd=str(workdir_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.TimeoutError as error:
            process.kill()
            raise validation_error(f'{runtime}_script 任務逾時（{timeout_seconds}s）') from error

        stdout_text = stdout_raw.decode('utf-8', errors='ignore')[:SCRIPT_OUTPUT_PREVIEW_MAX_CHARS]
        stderr_text = stderr_raw.decode('utf-8', errors='ignore')[:SCRIPT_OUTPUT_PREVIEW_MAX_CHARS]
        if process.returncode != 0:
            raise validation_error(f'{runtime}_script 執行失敗: {stderr_text or "未知錯誤"}')
        return {'return_code': process.returncode, 'stdout_preview': stdout_text, 'stderr_preview': stderr_text}

    def _resolve_workdir(self, *, payload: dict) -> Path:
        # 目的：解析並驗證任務工作目錄。
        # 為什麼：限制腳本執行邊界，避免排程任務存取未授權路徑。
        raw_workdir = str(payload.get('workdir') or '').strip()
        workspace_root = Path(__file__).resolve().parents[3]
        if not raw_workdir:
            target_path = workspace_root
        else:
            target_path = Path(raw_workdir)
        if not target_path.is_absolute():
            target_path = workspace_root / str(target_path)
        normalized_path = target_path.resolve()
        if not normalized_path.exists() or not normalized_path.is_dir():
            raise validation_error('workdir 不存在或不是目錄')
        self._ensure_path_in_allowed_roots(target_path=normalized_path)
        return normalized_path

    def _resolve_script_file_path(self, *, payload: dict) -> Path:
        # 目的：解析並驗證任務腳本檔案路徑。
        # 為什麼：避免任務執行任意系統檔案，降低錯誤與濫用風險。
        raw_file_path = str(payload.get('file_path') or '').strip()
        if not raw_file_path:
            raise validation_error('script 任務缺少 file_path')
        workspace_root = Path(__file__).resolve().parents[3]
        target_path = Path(raw_file_path)
        if not target_path.is_absolute():
            target_path = workspace_root / raw_file_path
        normalized_path = target_path.resolve()
        if not normalized_path.exists() or not normalized_path.is_file():
            raise validation_error('file_path 不存在或不是檔案')
        self._ensure_path_in_allowed_roots(target_path=normalized_path)
        return normalized_path

    def _ensure_path_in_allowed_roots(self, *, target_path: Path) -> None:
        # 目的：確認路徑在白名單目錄內。
        # 為什麼：確保 scheduler 腳本只能在授權範圍執行。
        resolved_target = target_path.resolve()
        for allowed_root in self._get_allowed_script_roots():
            try:
                resolved_target.relative_to(allowed_root)
                return
            except Exception:
                continue
        allowed_text = ', '.join(str(path) for path in self._get_allowed_script_roots())
        raise validation_error(f'路徑不在允許範圍內（{allowed_text}）')

    def _get_allowed_script_roots(self) -> list[Path]:
        # 目的：取得腳本執行白名單目錄。
        # 為什麼：讓部署可用設定控制執行邊界，不把路徑策略寫死在程式。
        workspace_root = Path(__file__).resolve().parents[3]
        raw_setting = str(getattr(settings, 'SCHEDULER_SCRIPT_ALLOWED_ROOTS', '') or '').strip()
        if not raw_setting:
            return [workspace_root]
        output: list[Path] = []
        for item in raw_setting.split(','):
            token = str(item or '').strip()
            if not token:
                continue
            root_path = Path(token)
            if not root_path.is_absolute():
                root_path = workspace_root / token
            resolved = root_path.resolve()
            if resolved.exists() and resolved.is_dir():
                output.append(resolved)
        if output:
            return output
        return [workspace_root]

    def _validate_bash_command(self, *, command_text: str) -> None:
        # 目的：驗證 bash 指令是否符合白名單與安全限制。
        # 為什麼：避免複合指令與重導向造成高風險系統操作。
        normalized_command = str(command_text or '').strip()
        if not normalized_command:
            raise validation_error('bash_script 任務缺少 command')
        if any(token in normalized_command for token in UNSAFE_BASH_TOKENS):
            raise validation_error('bash_script command 含不允許的特殊運算子')
        allowed_prefixes = self._get_allowed_bash_prefixes()
        if not any(normalized_command.startswith(prefix) for prefix in allowed_prefixes):
            prefix_text = ', '.join(allowed_prefixes)
            raise validation_error(f'bash_script command 不在允許前綴（{prefix_text}）')

    def _get_allowed_bash_prefixes(self) -> list[str]:
        raw_setting = str(getattr(settings, 'SCHEDULER_BASH_ALLOWED_PREFIXES', '') or '').strip()
        if not raw_setting:
            return ['python ', 'python3 ', 'node ', 'npm ', 'bash ', 'sh ', './']
        output = [str(item or '').strip() for item in raw_setting.split(',') if str(item or '').strip()]
        if output:
            return output
        return ['python ', 'python3 ', 'node ', 'npm ', 'bash ', 'sh ', './']

    def _dispatch_run_to_celery(self, *, task_id: str, run_id: str) -> bool:
        try:
            from src.worker.tasks import execute_task_run

            execute_task_run.delay(run_id=run_id)
            return True
        except Exception as error:
            logger.warning('scheduler_dispatch_to_celery_failed', task_id=task_id, run_id=run_id, error=str(error)[:300])
            return False

    def _sync_schedule_entry(self, *, task: ScheduledTask) -> None:
        task_key = self._build_redbeat_key(task_id=str(task.id))
        if not bool(task.enabled):
            self._remove_schedule_entry(task_key=task_key)
            return
        try:
            from celery.schedules import crontab
            from redbeat import RedBeatSchedulerEntry

            from src.worker.celery_app import celery_app

            minute, hour, day_of_month, month_of_year, day_of_week = self._parse_cron_fields(cron_expression=str(task.cron_expression or ''))
            schedule = crontab(minute=minute, hour=hour, day_of_month=day_of_month, month_of_year=month_of_year, day_of_week=day_of_week)
            entry = RedBeatSchedulerEntry(
                name=task_key,
                task='src.worker.tasks.trigger_scheduled_task',
                schedule=schedule,
                args=[str(task.id)],
                kwargs={},
                app=celery_app,
                options={'queue': str(getattr(settings, 'CELERY_QUEUE_NAME', 'dyagent.scheduler') or 'dyagent.scheduler')},
            )
            entry.save()
        except Exception as error:
            logger.warning('scheduler_sync_redbeat_failed', task_id=str(task.id), error=str(error)[:300])

    def _remove_schedule_entry(self, *, task_key: str) -> None:
        try:
            from redbeat import RedBeatSchedulerEntry

            from src.worker.celery_app import celery_app

            redis_entry_key = RedBeatSchedulerEntry.generate_key(celery_app, task_key)
            entry = RedBeatSchedulerEntry.from_key(redis_entry_key, app=celery_app)
            entry.delete()
        except Exception:
            return

    def _normalize_task_payload(self, *, db: Session, payload: dict, partial: bool = False) -> dict:
        if not isinstance(payload, dict):
            raise validation_error('payload 格式錯誤')
        output: dict = {}
        template_row = None
        if (not partial) or ('template_id' in payload):
            template_id = str(payload.get('template_id') or '').strip()
            if template_id:
                template_row = self._get_template_or_error(db=db, template_id=template_id)
                if not bool(template_row.enabled):
                    raise validation_error('指定模板未啟用')
                output['template_id'] = str(template_row.id)
            elif 'template_id' in payload:
                output['template_id'] = None

        if (not partial) or ('name' in payload):
            name = str(payload.get('name') or '').strip()
            if not name:
                raise validation_error('name 不可為空')
            output['name'] = name
        if (not partial) or ('description' in payload):
            output['description'] = str(payload.get('description') or '').strip() or None
        if (not partial) or ('cron_expression' in payload):
            cron_expression = str(payload.get('cron_expression') or '').strip()
            self._validate_cron_expression(cron_expression=cron_expression)
            output['cron_expression'] = cron_expression
        if (not partial) or ('timezone' in payload):
            output['timezone'] = str(payload.get('timezone') or 'Asia/Taipei').strip() or 'Asia/Taipei'

        if (not partial) or ('task_type' in payload) or (template_row is not None):
            task_type = str(payload.get('task_type') or '').strip()
            if template_row is not None:
                task_type = str(template_row.executor_type)
            if task_type not in ALLOWED_EXECUTOR_TYPES:
                raise validation_error('executor_type 不支援')
            output['task_type'] = task_type

        if (not partial) or ('payload' in payload) or (template_row is not None):
            input_payload = payload.get('payload') if isinstance(payload.get('payload'), dict) else {}
            if template_row is not None:
                default_payload = template_row.default_payload if isinstance(template_row.default_payload, dict) else {}
                merged_payload = dict(default_payload)
                merged_payload.update(input_payload)
                input_payload = merged_payload
                self._validate_payload_schema(
                    payload_schema=template_row.payload_schema,
                    payload=input_payload,
                    executor_type=str(template_row.executor_type or ''),
                )
            output['payload'] = input_payload

        if (not partial) or ('enabled' in payload):
            output['enabled'] = bool(payload.get('enabled', True))
        return output

    def _normalize_template_payload(self, *, payload: dict, partial: bool = False) -> dict:
        if not isinstance(payload, dict):
            raise validation_error('payload 格式錯誤')
        output: dict = {}
        if (not partial) or ('template_key' in payload):
            template_key = str(payload.get('template_key') or '').strip()
            if not template_key:
                raise validation_error('template_key 不可為空')
            output['template_key'] = template_key
        if (not partial) or ('name' in payload):
            name = str(payload.get('name') or '').strip()
            if not name:
                raise validation_error('name 不可為空')
            output['name'] = name
        if (not partial) or ('description' in payload):
            output['description'] = str(payload.get('description') or '').strip() or None
        if (not partial) or ('executor_type' in payload):
            executor_type = str(payload.get('executor_type') or '').strip()
            if executor_type not in ALLOWED_EXECUTOR_TYPES:
                raise validation_error('executor_type 不支援')
            output['executor_type'] = executor_type
        if (not partial) or ('payload_schema' in payload):
            output['payload_schema'] = payload.get('payload_schema') if isinstance(payload.get('payload_schema'), dict) else {}
        if (not partial) or ('default_payload' in payload):
            output['default_payload'] = payload.get('default_payload') if isinstance(payload.get('default_payload'), dict) else {}
        if (not partial) or ('enabled' in payload):
            output['enabled'] = bool(payload.get('enabled', True))
        return output

    def _validate_payload_schema(self, *, payload_schema: dict | None, payload: dict, executor_type: str) -> None:
        # 目的：檢查模板必填欄位並處理執行器相容規則。
        # 為什麼：歷史模板可能要求 content_id，但衛教 rotate 模式不需此欄位，需避免阻擋合法新流程。
        schema_obj = payload_schema if isinstance(payload_schema, dict) else {}
        required_fields = schema_obj.get('required') if isinstance(schema_obj.get('required'), list) else []
        normalized_executor_type = str(executor_type or '').strip()
        dispatch_mode = str(payload.get('dispatch_mode') or '').strip().lower()
        for required_field in required_fields:
            key = str(required_field or '').strip()
            if not key:
                continue
            if normalized_executor_type == 'health_education_dispatch' and dispatch_mode == 'rotate' and key == 'content_id':
                continue
            if payload.get(key) is None:
                raise validation_error(f'payload 缺少必要欄位: {key}')

    def _validate_cron_expression(self, *, cron_expression: str) -> None:
        parts = [item for item in str(cron_expression or '').split(' ') if item]
        if len(parts) != 5:
            raise validation_error('cron_expression 必須為 5 段 Crontab')
        for part in parts:
            if not CRON_PATTERN.fullmatch(part):
                raise validation_error('cron_expression 格式錯誤')

    def _parse_cron_fields(self, *, cron_expression: str) -> tuple[str, str, str, str, str]:
        self._validate_cron_expression(cron_expression=cron_expression)
        minute, hour, day_of_month, month_of_year, day_of_week = (item for item in cron_expression.split(' ') if item)
        return minute, hour, day_of_month, month_of_year, day_of_week

    def _get_task_or_error(self, *, db: Session, task_id: str) -> ScheduledTask:
        row = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if row is None:
            raise not_found_error('ScheduledTask', task_id)
        return row

    def _get_template_or_error(self, *, db: Session, template_id: str) -> ScheduledTaskTemplate:
        row = db.query(ScheduledTaskTemplate).filter(ScheduledTaskTemplate.id == template_id).first()
        if row is None:
            raise not_found_error('ScheduledTaskTemplate', template_id)
        return row

    def _serialize_task(self, *, task: ScheduledTask) -> dict:
        return {
            'id': str(task.id),
            'name': str(task.name or ''),
            'description': str(task.description or ''),
            'cron_expression': str(task.cron_expression or ''),
            'timezone': str(task.timezone or 'Asia/Taipei'),
            'task_type': str(task.task_type or ''),
            'executor_type': str(task.task_type or ''),
            'template_id': str(task.template_id) if getattr(task, 'template_id', None) else None,
            'payload': task.payload if isinstance(task.payload, dict) else {},
            'enabled': bool(task.enabled),
            'source': str(task.source or ''),
            'created_at': task.created_at.isoformat() if task.created_at else None,
            'updated_at': task.updated_at.isoformat() if task.updated_at else None,
        }

    def _serialize_template(self, *, row: ScheduledTaskTemplate) -> dict:
        return {
            'id': str(row.id),
            'template_key': str(row.template_key or ''),
            'name': str(row.name or ''),
            'description': str(row.description or ''),
            'executor_type': str(row.executor_type or ''),
            'payload_schema': row.payload_schema if isinstance(row.payload_schema, dict) else {},
            'default_payload': row.default_payload if isinstance(row.default_payload, dict) else {},
            'enabled': bool(row.enabled),
            'updated_at': row.updated_at.isoformat() if row.updated_at else None,
        }

    def _create_run_row(self, *, db: Session, task_id: str, trigger_source: str, input_payload: dict, executed_by_user_id: str | None) -> ScheduledTaskRun:
        run_row = ScheduledTaskRun(task_id=task_id, trigger_source=trigger_source, status='queued', input_payload=input_payload, output_payload={}, executed_by_user_id=executed_by_user_id)
        db.add(run_row)
        db.commit()
        db.refresh(run_row)
        return run_row

    def _serialize_run(self, *, run_row: ScheduledTaskRun) -> dict:
        return {
            'id': str(run_row.id),
            'task_id': str(run_row.task_id),
            'trigger_source': str(run_row.trigger_source or ''),
            'status': str(run_row.status or ''),
            'started_at': run_row.started_at.isoformat() if run_row.started_at else None,
            'finished_at': run_row.finished_at.isoformat() if run_row.finished_at else None,
            'duration_ms': run_row.duration_ms,
            'input_payload': run_row.input_payload if isinstance(run_row.input_payload, dict) else {},
            'output_payload': run_row.output_payload if isinstance(run_row.output_payload, dict) else {},
            'error_message': str(run_row.error_message or ''),
            'created_at': run_row.created_at.isoformat() if run_row.created_at else None,
        }

    def _build_task_alerts(self, *, task: ScheduledTask, run_rows: list[ScheduledTaskRun]) -> list[dict]:
        # 目的：依任務類型與執行紀錄產生可讀告警。
        # 為什麼：讓前端不需理解每種 output_payload 細節，也能直接提示營運關注風險。
        alert_items: list[dict] = []
        if str(task.task_type or '').strip() == 'health_education_dispatch':
            rotate_alert = self._build_health_education_rotate_alert(run_rows=run_rows)
            if rotate_alert is not None:
                alert_items.append(rotate_alert)
        return alert_items

    def _build_health_education_rotate_alert(self, *, run_rows: list[ScheduledTaskRun]) -> dict | None:
        # 目的：檢查衛教輪替任務是否連續無可發文章。
        # 為什麼：若長時間 no_eligible_content，通常代表候選池不足，需要人工補文或放寬條件。
        consecutive_empty_runs = 0
        for run_row in run_rows:
            output_payload = run_row.output_payload if isinstance(run_row.output_payload, dict) else {}
            dispatch_mode = str(output_payload.get('dispatch_mode') or '').strip().lower()
            reason = str(output_payload.get('reason') or '').strip().lower()
            if dispatch_mode != 'rotate':
                break
            if reason != 'no_eligible_content':
                break
            consecutive_empty_runs += 1
        alert_threshold = self._get_health_education_rotate_empty_alert_threshold()
        if consecutive_empty_runs < alert_threshold:
            return None
        return {
            'code': 'health_education_rotate_no_eligible_content',
            'level': 'warning',
            'consecutive_runs': consecutive_empty_runs,
            'message': f'衛教輪替已連續 {consecutive_empty_runs} 次無可發文章，請補充核准內容或調整冷卻天數。',
        }

    def _get_health_education_rotate_empty_alert_threshold(self) -> int:
        # 目的：取得衛教輪替空轉告警門檻。
        # 為什麼：讓營運可透過環境變數調整敏感度，不需改程式就能因應不同排程頻率。
        try:
            raw_threshold = int(getattr(settings, 'HEALTH_EDUCATION_ROTATE_EMPTY_ALERT_THRESHOLD', DEFAULT_HEALTH_EDUCATION_ROTATE_EMPTY_ALERT_THRESHOLD))
        except Exception:
            return DEFAULT_HEALTH_EDUCATION_ROTATE_EMPTY_ALERT_THRESHOLD
        return max(1, min(raw_threshold, MAX_HEALTH_EDUCATION_ROTATE_EMPTY_ALERT_THRESHOLD))

    def _build_redbeat_key(self, *, task_id: str) -> str:
        return f'dyagent:scheduled-task:{task_id}'

    def _hhmm_to_cron(self, hhmm: str) -> str:
        parts = str(hhmm or '').split(':')
        if len(parts) != 2:
            return '0 0 * * *'
        try:
            hour = max(0, min(23, int(parts[0])))
            minute = max(0, min(59, int(parts[1])))
        except Exception:
            return '0 0 * * *'
        return f'{minute} {hour} * * *'


scheduler_task_service = SchedulerTaskService()
