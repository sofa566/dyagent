from sqlalchemy import Column, String, DateTime, ForeignKey, Enum, Text, JSON, Integer, Boolean, Numeric
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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)
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
    # 代理者是否為主代理（Router）
    is_router = Column(Boolean, nullable=False, default=False)
    model_config = Column(JSON, default=dict)
    function_profile_id = Column(GUID(), ForeignKey('function_profiles.id'), nullable=True)
    mcp_config = Column(JSON, default=dict)
    skills = Column(JSON, default=list)
    tools = Column(JSON, default=list)
    rag_config = Column(JSON, default=dict)
    workspace_id = Column(GUID(), ForeignKey('workspaces.id'), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 簡化關聯以避免測試環境重複映射衝突（如需反向關聯，於查詢層處理）


class Conversation(Base):
    __tablename__ = 'conversations'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id = Column(GUID(), ForeignKey('users.id'), nullable=True)  # 一般使用者的會話歸屬
    agent_id = Column(GUID(), ForeignKey('agents.id'), nullable=False)
    title = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_interacted_at = Column(DateTime, default=datetime.utcnow)
    # 簡化：避免在測試環境建立 ORM 關聯，改以查詢層透過外鍵進行串接


class Message(Base):
    __tablename__ = 'messages'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(GUID(), ForeignKey('conversations.id'), nullable=False)
    role = Column(Enum('user', 'assistant', name='message_role'), nullable=False)
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

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
    uploaded_at = Column(DateTime, default=datetime.utcnow)

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
    timestamp = Column(DateTime, default=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 全域 Skill 註冊表（由管理者維護）
class SkillEntry(Base):
    __tablename__ = 'skills'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)
    description = Column(Text, default='')
    enabled = Column(Boolean, default=True)
    # 執行類型：webhook 或 python（預設 webhook）
    type = Column(Enum('webhook', 'python', name='skill_type'), nullable=False, default='webhook')
    # webhook 參數
    endpoint_url = Column(Text, nullable=True)
    http_method = Column(String(8), nullable=False, default='POST')
    headers = Column(JSON, default=dict)
    timeout_ms = Column(Integer, nullable=False, default=8000)
    # python handler：package.module:function
    python_handler = Column(String(255), nullable=True)
    # 輸入結構（JSON Schema）
    input_schema = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# 全域函式協定模板（Functions）
class FunctionProfile(Base):
    __tablename__ = 'function_profiles'
    __table_args__ = {'extend_existing': True}

    id = Column(GUID(), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)
    provider = Column(String(50), nullable=True)
    template = Column(Text, nullable=False)
    description = Column(Text, default='')
    enabled = Column(Boolean, default=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    created_at = Column(DateTime, default=datetime.utcnow)
