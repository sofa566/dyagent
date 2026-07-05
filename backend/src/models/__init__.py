from sqlalchemy import Column, String, DateTime, ForeignKey, Enum, Text, JSON, Integer, Boolean, Numeric, LargeBinary
from sqlalchemy.types import CHAR, TypeDecorator
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

from src.core.database import Base


# Cross-dialect UUID type that works on SQLite and PostgreSQL
class GUID(TypeDecorator):
    """Platform-independent GUID/UUID type.

    Uses PostgreSQL's UUID type when available; otherwise stores as CHAR(36).
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'postgresql':
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == 'postgresql':
            return value
        if isinstance(value, uuid.UUID):
            return str(value)
        return str(uuid.UUID(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


class User(Base):
    __tablename__ = 'users'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    role = Column(Enum('admin', 'agent_admin', 'user', name='user_role'), nullable=False, default='user')
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 提醒：如需存取使用者日誌，請於查詢層以 user_id 過濾 Log 表（為避免測試環境多重映射，暫不在此建立關聯）


class Role(Base):
    __tablename__ = 'roles'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(Enum('admin', 'agent_admin', 'user', name='role_name'), nullable=False, unique=True)
    permissions = Column(JSON, default=list)


class Workspace(Base):
    __tablename__ = 'workspaces'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(50), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    # 為降低測試環境之映射衝突風險，暫不在此建立到 Agent 的關聯
    # 如需查詢某工作區的代理者，請於查詢層以 workspace_id 過濾 Agent 表


class Agent(Base):
    __tablename__ = 'agents'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text, default='')
    system_prompt = Column(Text, nullable=True)
    model_type = Column(Enum('local', 'cloud', name='model_type'), nullable=False)
    agent_class = Column(Enum('master', 'public', 'tasked', name='agent_class_enum'), nullable=False, default='tasked')
    enabled = Column(Boolean, nullable=False, default=True)
    # 代理者是否為主代理（Router）
    is_router = Column(Boolean, nullable=False, default=False)
    model_config = Column(JSON, default=dict)
    function_profile_id = Column(GUID(), ForeignKey('function_profiles.id'), nullable=True)
    mcp_config = Column(JSON, default=dict)
    skills = Column(JSON, default=list)
    tools = Column(JSON, default=list)
    rag_config = Column(JSON, default=dict)
    workspace_id = Column(GUID(), ForeignKey('workspaces.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 簡化關聯以避免測試環境重複映射衝突（如需反向關聯，於查詢層處理）


class Conversation(Base):
    __tablename__ = 'conversations'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)  # 一般使用者的會話歸屬
    agent_id = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    title = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    last_interacted_at = Column(DateTime, default=datetime.now)
    # 簡化：避免在測試環境建立 ORM 關聯，改以查詢層透過外鍵進行串接


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey('conversations.id'), nullable=False)
    role = Column(Enum('user', 'assistant', name='message_role'), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.now)

    # 以 conversation_id 關聯，測試中不建立 ORM relationship，避免重複映射


class Document(Base):
    __tablename__ = 'documents'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    agent_id = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_type = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False, default='uploaded')
    last_error = Column(Text, nullable=True)
    indexed_at = Column(DateTime, nullable=True)
    uploaded_at = Column(DateTime, default=datetime.now)

    # 同上：查詢時以 agent_id 過濾


class Log(Base):
    __tablename__ = 'logs'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    level = Column(Enum('debug', 'info', 'warning', 'error', name='log_level'), nullable=False)
    action = Column(String(100), nullable=False)
    resource_type = Column(String(50), nullable=True)
    resource_id = Column(GUID(), nullable=True)
    details = Column(JSON, default=dict)
    ip_address = Column(String(45), nullable=True)
    timestamp = Column(DateTime, default=datetime.now)

    # 反向關聯由 User.logs 的 backref 提供


# 全域 MCP 連線註冊表（由管理者維護）
class MCPConnection(Base):
    __tablename__ = 'mcp'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, default='')
    enabled = Column(Boolean, default=True)
    # 傳輸模式：remote（HTTP/WS，預設）或 stdio（後續可擴充）
    transport = Column(Enum('remote', 'stdio', name='mcp_transport'), nullable=False, default='remote')
    # 遠端直連欄位
    base_url = Column(String(500), nullable=True)
    auth = Column(JSON, default=dict)            # { token?, api_key?, headers? }
    progress_field = Column(String(200), nullable=True)
    eta_field = Column(String(200), nullable=True)
    # 本機啟動（預留，未實作）
    command = Column(String(300), nullable=True)
    args = Column(JSON, default=list)
    env = Column(JSON, default=dict)
    # 輸入結構（JSON Schema，可選，用於表單渲染與驗證）
    input_schema = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# 全域 Skill 註冊表（由管理者維護）- 相容 Claude Skills
class SkillEntry(Base):
    __tablename__ = 'skills'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, default='')
    enabled = Column(Boolean, default=True)
    # 執行類型：webhook 或 python（預設 webhook）- 舊版欄位
    type = Column(Enum('webhook', 'python', name='skill_type_enum'), nullable=False, default='webhook')
    # webhook 參數
    endpoint_url = Column(Text, nullable=True)
    http_method = Column(String(8), nullable=False, default='POST')
    headers = Column(JSON, default=dict)
    timeout_ms = Column(Integer, nullable=False, default=8000)
    # python handler：package.module:function
    python_handler = Column(String(255), nullable=True)
    # executable command：例如 uvx/npx/java/python
    command = Column(Text, nullable=True)
    # 輸入結構（JSON Schema）
    input_schema = Column(JSON, default=dict)
    # Claude Skills 相容欄位
    skill_type = Column(String(20), nullable=True, default='executable')  # prompt | executable | hybrid
    prompt_template = Column(Text, nullable=True)  # SKILL.md 內容（提示詞模板）
    zip_bundle = Column(LargeBinary, nullable=True)  # 完整 ZIP 檔案
    references = Column(JSON, nullable=True)  # 解壓後的 references/ 內容（JSON 快取）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# 全域函式協定模板（Functions）- 相容 OpenAI Function Calling
class FunctionProfile(Base):
    __tablename__ = 'function_profiles'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)
    provider = Column(String(50), nullable=True)
    template = Column(Text, nullable=True)  # 舊版提示詞模板（向後相容）
    description = Column(Text, default='')
    enabled = Column(Boolean, default=True)
    version = Column(Integer, nullable=False, default=1)
    references = Column(JSON, nullable=True)  # Claude Skill 的 references/ 內容
    # OpenAI Function Calling 格式欄位
    parameters = Column(JSON, nullable=True)  # OpenAI JSON Schema 格式
    handler_type = Column(String(20), nullable=True, default='internal')  # internal | webhook | mcp
    handler_config = Column(JSON, nullable=True)  # 執行配置（endpoint URL、MCP server 等）
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# RAG 資料集註冊表（公有/代理者私有）
class RagDataset(Base):
    __tablename__ = 'rag_datasets'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(150), nullable=False)
    scope = Column(Enum('global', 'agent_private', name='rag_scope'), nullable=False)
    agent_id = Column(GUID(), ForeignKey('agents.id'), nullable=True)
    owner_user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)
    sensitivity = Column(Enum('normal', 'confidential', 'restricted', name='rag_sensitivity'), nullable=False, default='normal')
    vector_backend = Column(String(50), nullable=True)
    index_name = Column(String(150), nullable=True)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# 多代理協作 Session（Orchestrator ReAct 生命週期）
class MultiAgentSession(Base):
    """職責：記錄一次多代理協作的完整生命週期，包含分解計畫、ReAct 步驟、合成結果與自評。
    存在原因：與單代理 Conversation 分開，避免污染既有訊息層，並支援重試與可觀測性。
    """
    __tablename__ = 'multi_agent_sessions'
    __table_args__ = {'extend_existing': True}

    id              = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey('conversations.id'), nullable=False)
    router_agent_id = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    user_message    = Column(Text, nullable=False)
    status          = Column(
                        Enum('planning', 'running', 'synthesizing', 'evaluating', 'done', 'failed',
                             name='mas_status'),
                        nullable=False, default='planning')
    react_step      = Column(Integer, nullable=False, default=0)
    max_steps       = Column(Integer, nullable=False, default=3)
    plan_json       = Column(JSON, default=dict)    # 本輪 LLM 分解計畫（每次重試覆寫）
    synthesis       = Column(Text, nullable=True)   # 最終合成回覆
    eval_ok         = Column(Boolean, nullable=True)  # 最後一次自評結果
    created_at      = Column(DateTime, default=datetime.now)
    updated_at      = Column(DateTime, default=datetime.now, onupdate=datetime.now)


# 多代理協作子任務（每個 Session 可有多個 Task）
class MultiAgentTask(Base):
    """職責：記錄 Orchestrator 分配給單一子代理的任務描述、執行狀態與輸出結果。
    存在原因：正規化子任務資料，支援依賴關係、失敗追蹤與合成輸入。
    """
    __tablename__ = 'multi_agent_tasks'
    __table_args__ = {'extend_existing': True}

    id          = Column(GUID(), primary_key=True, default=uuid.uuid4)
    session_id  = Column(GUID(), ForeignKey('multi_agent_sessions.id'), nullable=False)
    task_index  = Column(Integer, nullable=False)     # 本輪中的序號（0-based）
    agent_id    = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    task_desc   = Column(Text, nullable=False)         # 注入前置背景後的任務描述
    depends_on  = Column(JSON, default=list)           # [task_index, ...] 前置依賴
    status      = Column(
                    Enum('pending', 'running', 'done', 'failed', 'skipped',
                         name='mat_status'),
                    nullable=False, default='pending')
    result_text = Column(Text, nullable=True)          # 子代理完整輸出文字
    error       = Column(Text, nullable=True)
    started_at  = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


# LLM 每輪審計紀錄
class LlmTurn(Base):
    __tablename__ = 'llm_turns'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey('conversations.id'), nullable=False)
    agent_id = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    message_user_id = Column(GUID(), ForeignKey('messages.id'), nullable=True)
    message_assistant_id = Column(GUID(), ForeignKey('messages.id'), nullable=True)
    provider = Column(String(50), nullable=True)
    model = Column(String(120), nullable=True)
    tier = Column(String(20), nullable=True)
    system_prompt_snapshot = Column(Text, nullable=True)
    context_snapshot = Column(JSON, default=dict)
    usage = Column(JSON, default=dict)
    cost_usd = Column(Numeric(12, 6), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    status = Column(String(20), nullable=False, default='success')
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class SkillInteraction(Base):
    """目的：儲存 HTML 技能的多步驟互動狀態。
    為什麼：executable skill 每次為新 subprocess，需將流程狀態持久化以支援 submit/back/resume。
    """
    __tablename__ = 'skill_interactions'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey('conversations.id'), nullable=False)
    tool_name = Column(String(100), nullable=False)
    skill_id = Column(GUID(), ForeignKey('skills.id'), nullable=True)
    status = Column(String(20), nullable=False, default='active')
    current_step = Column(String(50), nullable=True)
    state_json = Column(JSON, default=dict)
    ui_session_nonce = Column(String(120), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ChatAttachment(Base):
    """目的：保存聊天附件的二進位檔案 metadata。
    為什麼：聊天請求僅傳遞 attachment_id，工具執行時再依需求載入與轉換內容。
    """

    __tablename__ = 'chat_attachments'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey('conversations.id'), nullable=True)
    user_id = Column(GUID(), ForeignKey('users.id'), nullable=False)
    filename = Column(String(255), nullable=False)
    ext = Column(String(20), nullable=False, default='')
    mime_type = Column(String(120), nullable=False, default='application/octet-stream')
    file_path = Column(String(700), nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    status = Column(String(20), nullable=False, default='uploaded')
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)
