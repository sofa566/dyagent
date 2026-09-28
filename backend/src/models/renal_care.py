from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from src.core.database import Base
from src.models import GUID


class RenalPatient(Base):
    # 目的：保存腎友照護 POC 的病患主檔資料。
    # 為什麼：S01/S02、追蹤與儀表板都需統一病患主鍵，避免資料來源分散。
    __tablename__ = 'renal_patients'
    __table_args__ = (
        UniqueConstraint('patient_code', name='uq_renal_patients_patient_code'),
        UniqueConstraint('line_user_id', name='uq_renal_patients_line_user_id'),
        {'extend_existing': True},
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    patient_code = Column(String(24), nullable=False)
    display_name = Column(String(100), nullable=False)
    tel_no = Column(String(32), nullable=True)
    id_card = Column(String(32), nullable=True)
    med_history = Column(Text, nullable=True)
    age = Column(String(20), nullable=True)
    diagnosis = Column(String(255), nullable=True)
    primary_nurse_id = Column(String(32), nullable=True)
    is_diabetic = Column(Boolean, nullable=False, default=False)
    dry_weight_kg = Column(Numeric(8, 2), nullable=True)
    line_user_id = Column(String(128), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MonitoringRecord(Base):
    # 目的：保存日常監測資料與規則比對結果。
    # 為什麼：POC 需要回溯病患填報、比對結果與追蹤判定依據。
    __tablename__ = 'monitoring_records'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=False)
    record_type = Column(Enum('MORNING', 'EVENING', 'SYMPTOM_REPORT', name='monitoring_record_type_enum'), nullable=False)
    recorded_at = Column(DateTime, nullable=False)
    submitted_by_role = Column(Enum('PATIENT', 'FAMILY', 'CAREGIVER', 'NURSE', name='monitoring_submitted_role_enum'), nullable=False)
    measurements = Column(JSON, default=dict)
    symptoms = Column(JSON, default=dict)
    confirmed = Column(Boolean, nullable=False, default=False)
    record_status = Column(Enum('SAVED', 'REJECTED', name='monitoring_record_status_enum'), nullable=False, default='SAVED')
    comparison_result = Column(JSON, default=dict)
    matched_rules = Column(JSON, default=list)
    follow_up_required = Column(Boolean, nullable=False, default=False)
    patient_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class FollowUpCase(Base):
    # 目的：保存主護追蹤案件狀態。
    # 為什麼：日常監測與透析異常都需統一追蹤中心，供護理師接手與結案。
    __tablename__ = 'follow_up_cases'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=False)
    source_type = Column(Enum('monitoring', 'dialysis', name='follow_up_source_type_enum'), nullable=False)
    source_record_id = Column(String(64), nullable=True)
    severity = Column(Enum('info', 'warning', 'critical', name='follow_up_severity_enum'), nullable=False, default='warning')
    status = Column(Enum('OPEN', 'IN_PROGRESS', 'CLOSED', name='follow_up_status_enum'), nullable=False, default='OPEN')
    title = Column(String(255), nullable=False)
    details = Column(JSON, default=dict)
    assigned_nurse_id = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    closed_at = Column(DateTime, nullable=True)


class DialysisSession(Base):
    # 目的：保存單次透析療程主檔。
    # 為什麼：透析流程包含洗前、洗中、洗後多階段，需要單一會話主鍵串接事件。
    __tablename__ = 'dialysis_sessions'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=False)
    dialysis_date = Column(Date, nullable=False)
    shift = Column(Enum('MORNING', 'AFTERNOON', 'EVENING', name='dialysis_shift_enum'), nullable=False)
    bed_no = Column(String(20), nullable=False)
    machine_no = Column(String(30), nullable=False)
    pre_weight_kg = Column(Numeric(8, 2), nullable=True)
    dry_weight_kg = Column(Numeric(8, 2), nullable=True)
    target_uf_l = Column(Numeric(8, 2), nullable=True)
    post_weight_kg = Column(Numeric(8, 2), nullable=True)
    actual_uf_l = Column(Numeric(8, 2), nullable=True)
    completion_status = Column(
        Enum('CREATED', 'PRE_CHECK_CONFIRMED', 'IN_PROGRESS', 'COMPLETED', 'ENDED_EARLY', name='dialysis_completion_status_enum'),
        nullable=False,
        default='CREATED',
    )
    has_tourniquet = Column(Boolean, nullable=True)
    note = Column(Text, nullable=True)
    created_by = Column(String(32), nullable=True)
    confirmed_by = Column(String(32), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class DialysisEvent(Base):
    # 目的：保存透析中事件與處置記錄。
    # 為什麼：異常事件是追蹤與風險控管核心，需要獨立事件表。
    __tablename__ = 'dialysis_events'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    session_id = Column(GUID(), ForeignKey('dialysis_sessions.id'), nullable=False)
    event_type = Column(String(60), nullable=False)
    event_at = Column(DateTime, nullable=False)
    payload = Column(JSON, default=dict)
    handled_by = Column(String(32), nullable=True)
    action = Column(Text, nullable=True)
    follow_up_required = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now)


class PatientPortalLink(Base):
    # 目的：保存 LINE 免註冊入口連結狀態。
    # 為什麼：連結需一次性、可過期與可撤銷，才能符合最小安全需求。
    __tablename__ = 'patient_portal_links'
    __table_args__ = (
        UniqueConstraint('token_hash', name='uq_patient_portal_links_token_hash'),
        {'extend_existing': True},
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=False)
    line_user_id = Column(String(128), nullable=False)
    token_hash = Column(String(128), nullable=False)
    reason = Column(String(60), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    created_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class MonitoringReminderPolicy(Base):
    # 目的：定義腎友日常回報提醒政策。
    # 為什麼：將提醒條件配置化，後續可由同一機制支援不同病患或群組。
    __tablename__ = 'monitoring_reminder_policies'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=True)
    channel = Column(Enum('line', name='monitoring_reminder_channel_enum'), nullable=False, default='line')
    window = Column(Enum('MORNING', 'EVENING', name='monitoring_reminder_window_enum'), nullable=False)
    requires_glucose_for_diabetic = Column(Boolean, nullable=False, default=True)
    message_template = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MonitoringReminderJob(Base):
    # 目的：保存每次提醒派送任務。
    # 為什麼：提供重試、追蹤與排程對帳依據。
    __tablename__ = 'monitoring_reminder_jobs'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    policy_id = Column(GUID(), ForeignKey('monitoring_reminder_policies.id'), nullable=True)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=False)
    target_date = Column(Date, nullable=False)
    window = Column(Enum('MORNING', 'EVENING', name='monitoring_reminder_window_enum'), nullable=False)
    status = Column(Enum('pending', 'sent', 'failed', 'skipped', name='monitoring_reminder_job_status_enum'), nullable=False, default='pending')
    scheduled_at = Column(DateTime, nullable=False)
    processed_at = Column(DateTime, nullable=True)
    payload = Column(JSON, default=dict)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class MonitoringReminderDeliveryLog(Base):
    # 目的：保存提醒投遞結果。
    # 為什麼：需追溯每次 LINE 推送是否送達與失敗原因。
    __tablename__ = 'monitoring_reminder_delivery_logs'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    job_id = Column(GUID(), ForeignKey('monitoring_reminder_jobs.id'), nullable=False)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=False)
    line_user_id = Column(String(128), nullable=True)
    delivery_status = Column(Enum('sent', 'failed', 'skipped', name='monitoring_reminder_delivery_status_enum'), nullable=False)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class ScheduledTask(Base):
    # 目的：保存可由 Crontab 觸發的系統任務定義。
    # 為什麼：讓營運人員可透過後台動態配置啟動時間與任務內容，不再硬編碼於後端排程器。
    __tablename__ = 'scheduled_tasks'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    cron_expression = Column(String(120), nullable=False)
    timezone = Column(String(80), nullable=False, default='Asia/Taipei')
    task_type = Column(
        Enum(
            'http_call',
            'bash_script',
            'nodejs_script',
            'python_script',
            'renal_reminder_dispatch',
            'monitoring_backfill',
            'health_education_dispatch',
            name='scheduled_task_type_enum',
        ),
        nullable=False,
    )
    template_id = Column(GUID(), ForeignKey('scheduled_task_templates.id'), nullable=True)
    payload = Column(JSON, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    source = Column(String(30), nullable=False, default='manual')
    created_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    updated_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ScheduledTaskRun(Base):
    # 目的：記錄每次排程任務執行結果。
    # 為什麼：排程任務若失敗需可追溯輸入、輸出與錯誤摘要，供營運與工程排查。
    __tablename__ = 'scheduled_task_runs'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    task_id = Column(GUID(), ForeignKey('scheduled_tasks.id'), nullable=False)
    trigger_source = Column(Enum('scheduler', 'manual', 'api', name='scheduled_task_trigger_source_enum'), nullable=False, default='scheduler')
    status = Column(Enum('queued', 'running', 'success', 'failed', name='scheduled_task_run_status_enum'), nullable=False, default='queued')
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    input_payload = Column(JSON, default=dict)
    output_payload = Column(JSON, default=dict)
    error_message = Column(Text, nullable=True)
    executed_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class ScheduledTaskTemplate(Base):
    # 目的：保存可重用的任務模板（執行器 + schema + 預設 payload）。
    # 為什麼：讓使用者可定義任務類型語意，降低每次新增排程的重複設定成本。
    __tablename__ = 'scheduled_task_templates'
    __table_args__ = (
        UniqueConstraint('template_key', name='uq_scheduled_task_templates_template_key'),
        {'extend_existing': True},
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    template_key = Column(String(80), nullable=False)
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    executor_type = Column(
        Enum(
            'http_call',
            'bash_script',
            'nodejs_script',
            'python_script',
            'renal_reminder_dispatch',
            'monitoring_backfill',
            'health_education_dispatch',
            name='scheduled_task_type_enum',
        ),
        nullable=False,
    )
    payload_schema = Column(JSON, default=dict)
    default_payload = Column(JSON, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class HealthEducationContent(Base):
    # 目的：保存衛教候選內容與審核狀態。
    # 為什麼：讓 Agent/人工蒐集內容可進入可治理流程，避免直接發送造成風險。
    __tablename__ = 'health_education_contents'
    __table_args__ = (
        UniqueConstraint('source_url', name='uq_health_education_contents_source_url'),
        {'extend_existing': True},
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    title = Column(String(300), nullable=False)
    source_name = Column(String(120), nullable=True)
    source_url = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    tags = Column(JSON, default=list)
    status = Column(Enum('draft', 'approved', 'rejected', 'sent', name='health_education_status_enum'), nullable=False, default='draft')
    created_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    approved_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    last_sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class HealthEducationDeliveryLog(Base):
    # 目的：記錄每次衛教內容投遞結果。
    # 為什麼：提供 sent/failed/skipped 稽核追溯，支援營運檢視與重送判斷。
    __tablename__ = 'health_education_delivery_logs'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    content_id = Column(GUID(), ForeignKey('health_education_contents.id'), nullable=False)
    patient_id = Column(GUID(), ForeignKey('renal_patients.id'), nullable=False)
    line_user_id = Column(String(128), nullable=True)
    audience_rule = Column(String(60), nullable=False, default='all')
    trigger_source = Column(Enum('manual', 'scheduler', name='health_education_trigger_source_enum'), nullable=False, default='manual')
    status = Column(Enum('sent', 'failed', 'skipped', name='health_education_delivery_status_enum'), nullable=False)
    detail = Column(Text, nullable=True)
    payload = Column(JSON, default=dict)
    sent_at = Column(DateTime, default=datetime.now)
    created_at = Column(DateTime, default=datetime.now)


class HealthEducationSourceRule(Base):
    # 目的：維護衛教搜尋來源網域規則（trusted/blocked）。
    # 為什麼：來源治理需可由營運人員動態調整，不應只能改環境變數。
    __tablename__ = 'health_education_source_rules'
    __table_args__ = (
        UniqueConstraint('domain', name='uq_health_education_source_rules_domain'),
        {'extend_existing': True},
    )

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    domain = Column(String(255), nullable=False)
    policy = Column(Enum('trusted', 'blocked', name='health_education_source_policy_enum'), nullable=False, default='trusted')
    enabled = Column(Boolean, nullable=False, default=True)
    note = Column(String(255), nullable=True)
    created_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    updated_by_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
