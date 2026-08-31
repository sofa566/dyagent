import uuid

from fastapi import APIRouter, Body, Depends, Request, Response
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.api.errors import not_found_error, validation_error
from src.core.database import get_db
from src.core.logging import get_logger
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.models import (
    Agent,
    FunctionProfile,
    MCPConnection,
    RagDataset,
    SkillEntry,
    User,
    Workspace,
)
from src.services.access_control_service import access_control_service
from src.services.chat_router import ChatRouter
from src.services.embedding_service import embedding_service
from src.services.llm_client import LLMClient
from src.services.mcp_client import MCPClient
from src.services.qdrant_service import qdrant_service

router = APIRouter()
logger = get_logger(__name__)
AGENT_PRIVATE_DATASET_DEPRECATED_SUNSET = 'Tue, 31 Mar 2027 00:00:00 GMT'


def _mark_agent_private_dataset_api_deprecated(*, response: Response, operation: str, agent_id: str, dataset_id: str | None = None) -> None:
    # 目的：標記舊私有資料集 API 已進入退場期並提供替代路徑。
    # 為什麼：讓客戶端可在回應層感知 deprecated 訊號，逐步切換到 /api/rag/datasets。
    response.headers['Deprecation'] = 'true'
    response.headers['Sunset'] = AGENT_PRIVATE_DATASET_DEPRECATED_SUNSET
    response.headers['Link'] = '</api/rag/datasets>; rel="successor-version"'
    response.headers['X-Deprecated-Reason'] = 'use /api/rag/datasets with scope=agent_private'
    logger.warning(
        'agents.private_dataset_api_deprecated_called',
        operation=str(operation or ''),
        agent_id=str(agent_id or ''),
        dataset_id=str(dataset_id or ''),
    )


def _can_read_agent_integrations(current_user: User, db: Session) -> bool:
    """目的：統一判斷代理者整合設定的讀取權限。
    為什麼：US3 要求一般 user 具唯讀能力，需與 update 權限邏輯分離。
    """
    return bool(
        check_permission(current_user, 'read_agent', db=db)
        or check_permission(current_user, 'update_agent', db=db)
        or check_permission(current_user, 'chat', db=db)
    )


def _build_agent_execute_permission_key(agent_id: str) -> str:
    return f'entity.agent.{agent_id}.execute'


def _build_agent_execute_permission_keys(*, agent: Agent) -> set[str]:
    """目的：產生某代理可對應的 execute 權限鍵集合。
    為什麼：相容既有以 agent_id 或 agent_name 建立的權限鍵。
    """
    permission_keys: set[str] = set()
    agent_id = str(getattr(agent, 'id', '') or '').strip()
    if agent_id:
        permission_keys.add(_build_agent_execute_permission_key(agent_id))
    agent_name = str(getattr(agent, 'name', '') or '').strip()
    if agent_name:
        permission_keys.add(f'entity.agent.{agent_name}.execute')
    return permission_keys


def _build_dataset_execute_permission_keys(*, dataset: RagDataset) -> set[str]:
    # 目的：產生資料集可對應的 execute 權限鍵集合。
    # 為什麼：刪除資料集時需同步清理既有 id/name 形式的歷史權限鍵。
    permission_keys: set[str] = set()
    dataset_id = str(getattr(dataset, 'id', '') or '').strip()
    if dataset_id:
        permission_keys.add(f'entity.dataset.{dataset_id}.execute')
    dataset_name = str(getattr(dataset, 'name', '') or '').strip()
    if dataset_name:
        permission_keys.add(f'entity.dataset.{dataset_name}.execute')
    return permission_keys


def _resolve_user_private_agent_access_set(db: Session, current_user: User) -> set[str]:
    """目的：解析目前使用者可存取的 private agent execute 權限鍵集合。
    為什麼：private 類型代理僅允許具實體 execute 權限者可見與使用。
    """
    try:
        capability = access_control_service.resolve_effective_capability(db, current_user)
        permission_keys = {str(key) for key in (capability.permissions or [])}
    except Exception:
        permission_keys = set()

    private_agent_permissions: set[str] = set()
    for permission_key in permission_keys:
        if permission_key.startswith('entity.agent.') and permission_key.endswith('.execute'):
            private_agent_permissions.add(permission_key)
    return private_agent_permissions


@router.get('/agents/public')
async def list_public_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提供一般使用者可見的代理者清單（寬鬆模式）。

    - 僅需通過身份驗證；不再要求 `read_agent`/`chat` 權限，避免一般使用者 403。
    - 僅回傳基本資訊供前端選擇。
    """
    # 僅驗證登入；private 仍需綁定實體 execute 權限。
    private_access_set = _resolve_user_private_agent_access_set(db, current_user)
    agent_rows = db.query(Agent).filter(Agent.enabled == True).all()  # noqa: E712
    agents: list[Agent] = []
    for agent in agent_rows:
        agent_class = str(getattr(agent, 'agent_class', 'tasked') or 'tasked').strip().lower()
        if agent_class != 'private':
            agents.append(agent)
            continue
        permission_keys = _build_agent_execute_permission_keys(agent=agent)
        if any(key in private_access_set for key in permission_keys):
            agents.append(agent)
    agents = sorted(agents, key=lambda x: (0 if bool(getattr(x, 'is_router', False)) else 1, str(x.name or '')))
    return {
        'agents': [
            {
                'id': str(a.id),
                'name': a.name,
                'description': a.description,
                'is_router': bool(getattr(a, 'is_router', False)),
                'agent_class': str(getattr(a, 'agent_class', 'tasked') or 'tasked'),
                'enabled': bool(getattr(a, 'enabled', True)),
            }
            for a in agents
        ]
    }


@router.get('/agents/{agent_id}/integrations')
async def get_agent_integrations(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not _can_read_agent_integrations(current_user, db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 讀取 functions_definition_template（回退相容 toolcall_guide）
    tc_guide = ''
    provider = ''
    skill_examples: dict = {}
    try:
        cfg = agent.model_config or {}
        if isinstance(cfg, dict):
            if isinstance(cfg.get('functions_definition_template'), str):
                tc_guide = cfg.get('functions_definition_template') or ''
            elif isinstance(cfg.get('toolcall_guide'), str):
                tc_guide = cfg.get('toolcall_guide') or ''
        if isinstance(cfg, dict):
            # 優先直接 provider，其次 cloud.provider
            pv = cfg.get('provider')
            if not pv and isinstance(cfg.get('cloud'), dict):
                pv = cfg.get('cloud', {}).get('provider')
            if isinstance(pv, str):
                provider = pv
            if isinstance(cfg.get('skill_examples'), dict):
                skill_examples = cfg.get('skill_examples') or {}
    except Exception:
        tc_guide = ''
        provider = ''
        skill_examples = {}

    # 工具改為全域可用；此處回傳可用技能清單與 schema 供前端顯示。
    skills_out: list[str] = []
    skill_schemas: dict[str, dict] = {}
    try:
        rows = db.query(SkillEntry).filter(SkillEntry.enabled == True).all()  # noqa: E712
        for row in rows:
            skill_name = str(getattr(row, 'name', '') or '').strip()
            if not skill_name:
                continue
            skills_out.append(skill_name)
            if isinstance(getattr(row, 'input_schema', None), dict):
                skill_schemas[skill_name] = row.input_schema or {}
    except Exception:
        skills_out = []
        skill_schemas = {}

    # 蒐集全域啟用 MCP schema（工具公用，不再依 Agent 綁定）。
    mcp_schemas: dict[str, dict] = {}
    try:
        rows = db.query(MCPConnection).filter(MCPConnection.enabled == True).all()  # noqa: E712
        for row in rows:
            mcp_name = str(getattr(row, 'name', '') or '').strip()
            if not mcp_name:
                continue
            mcp_schemas[f'mcp:{mcp_name}'] = getattr(row, 'input_schema', {}) or {}
    except Exception:
        mcp_schemas = {}

    function_profile_id = None
    try:
        if isinstance(agent.model_config, dict):
            fpid = (agent.model_config or {}).get('function_profile_id')
            if isinstance(fpid, str) and fpid:
                function_profile_id = fpid
    except Exception:
        function_profile_id = None
    if function_profile_id is None and getattr(agent, 'function_profile_id', None) is not None:
        function_profile_id = str(agent.function_profile_id)

    return {
        'mcp_config': [],
        'skills': [],
        'rag_config': {},
        'functions_definition_template': tc_guide,
        'toolcall_guide': tc_guide,
        'model_provider': provider or '',
        'skill_examples': skill_examples,
        'skill_schemas': skill_schemas,
        'mcp_schemas': mcp_schemas,
        'mcp_ids': [],
        'skill_ids': [],
        'function_profile_id': function_profile_id,
        'rag_dataset_ids': {'global_dataset_ids': [], 'private_dataset_ids': []},
        'can_read': True,
        'can_update': bool(check_permission(current_user, 'update_agent', db=db)),
    }


@router.put('/agents/{agent_id}/integrations')
async def update_agent_integrations(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    before_model_config = dict(agent.model_config or {}) if isinstance(agent.model_config, dict) else {}
    before_snapshot = {
        'function_profile_id': before_model_config.get('function_profile_id'),
        'toolcall_guide': str(before_model_config.get('functions_definition_template') or before_model_config.get('toolcall_guide') or ''),
    }

    # 讀取欄位並做基本驗證
    function_profile_id = payload.get('function_profile_id') if isinstance(payload, dict) else None
    if function_profile_id is not None and not isinstance(function_profile_id, str):
        raise validation_error('function_profile_id 必須為字串')

    if isinstance(function_profile_id, str) and function_profile_id.strip():
        profile = db.query(FunctionProfile).filter(FunctionProfile.id == function_profile_id.strip(), FunctionProfile.enabled == True).first()  # noqa: E712
        if not profile:
            raise validation_error('function_profile_id 無效或未啟用')

    # 寫入 functions_definition_template（回退相容 toolcall_guide）至 model_config
    try:
        guide = None
        if isinstance(payload, dict):
            guide = payload.get('functions_definition_template')
            if guide is None:
                guide = payload.get('toolcall_guide')
        # 注意：JSON 欄位需避免原地修改，否則 ORM 可能不觸發 UPDATE
        base_cfg = agent.model_config if isinstance(agent.model_config, dict) else {}
        cfg = dict(base_cfg)
        if isinstance(guide, str):
            cfg['functions_definition_template'] = guide
            cfg['toolcall_guide'] = guide
        if isinstance(function_profile_id, str):
            cfg['function_profile_id'] = function_profile_id.strip() or None
        # 寫入 skills 的示例參數（限制於當前啟用/選取的 skills）
        sk_ex = (payload or {}).get('skill_examples') if isinstance(payload, dict) else None
        if isinstance(sk_ex, dict):
            # 僅保留目前仍啟用技能名稱；值可為字串(JSON)或物件
            enabled_names = {
                str(row.name or '').strip()
                for row in db.query(SkillEntry).filter(SkillEntry.enabled == True).all()  # noqa: E712
                if str(row.name or '').strip()
            }
            filtered = {}
            for k, v in sk_ex.items():
                if str(k or '').strip() in enabled_names and (isinstance(v, str | dict)):
                    filtered[k] = v
            cfg['skill_examples'] = filtered
        cfg.pop('mcp_ids', None)
        cfg.pop('skill_ids', None)
        agent.model_config = cfg
        flag_modified(agent, 'model_config')
    except Exception:
        pass
    try:
        if isinstance(function_profile_id, str):
            agent.function_profile_id = function_profile_id.strip() or None
    except Exception:
        pass
    db.commit()
    db.refresh(agent)

    # 簡易審計（以 Log 表）
    try:
        from src.models import Log
        after_model_config = dict(agent.model_config or {}) if isinstance(agent.model_config, dict) else {}
        after_snapshot = {
            'function_profile_id': after_model_config.get('function_profile_id'),
            'toolcall_guide': str(after_model_config.get('functions_definition_template') or after_model_config.get('toolcall_guide') or ''),
        }
        changed_fields: list[str] = []
        for field_name in before_snapshot.keys():
            if before_snapshot.get(field_name) != after_snapshot.get(field_name):
                changed_fields.append(field_name)
        log = Log(
            user_id=current_user.id,
            level='info',
            action='agent.integrations.update',
            resource_type='agent',
            resource_id=agent.id,
            details={
                'changed_fields': changed_fields,
                'before': before_snapshot,
                'after': after_snapshot,
            },
            ip_address=None,
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()

    return {
        'mcp_config': [],
        'skills': [],
        'rag_config': {},
        'functions_definition_template': ((agent.model_config or {}).get('functions_definition_template') if isinstance(agent.model_config, dict) else None) or ((agent.model_config or {}).get('toolcall_guide') if isinstance(agent.model_config, dict) else ''),
        'toolcall_guide': ((agent.model_config or {}).get('functions_definition_template') if isinstance(agent.model_config, dict) else None) or ((agent.model_config or {}).get('toolcall_guide') if isinstance(agent.model_config, dict) else ''),
        'mcp_ids': [],
        'skill_ids': [],
        'function_profile_id': (agent.model_config or {}).get('function_profile_id') if isinstance(agent.model_config, dict) else None,
    }


@router.get('/agents/{agent_id}/integrations/audit')
async def list_agent_integrations_audit(
    agent_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_logs', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    safe_limit = max(1, min(int(limit or 20), 100))
    from src.models import Log

    rows = db.query(Log).filter(
        Log.action == 'agent.integrations.update',
        Log.resource_type == 'agent',
        Log.resource_id == agent.id,
    ).order_by(Log.timestamp.desc()).limit(safe_limit).all()

    return {
        'entries': [
            {
                'id': str(row.id),
                'user_id': str(row.user_id) if row.user_id else None,
                'timestamp': row.timestamp.isoformat() if row.timestamp else None,
                'details': row.details if isinstance(row.details, dict) else {},
            }
            for row in rows
        ]
    }


@router.get('/agents/{agent_id}/prompt')
async def get_agent_prompt(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    source = 'default'
    value = ''
    if isinstance(getattr(agent, 'system_prompt', None), str) and agent.system_prompt.strip():
        source = 'agent'
        value = agent.system_prompt.strip()
    elif isinstance(agent.model_config, dict) and isinstance((agent.model_config or {}).get('system_prompt'), str) and (agent.model_config or {}).get('system_prompt').strip():
        source = 'model_config'
        value = (agent.model_config or {}).get('system_prompt').strip()
    elif isinstance(agent.description, str) and agent.description.strip():
        source = 'description'
        value = agent.description.strip()
    return {'agent_id': str(agent.id), 'system_prompt': value, 'source': source}


@router.put('/agents/{agent_id}/prompt')
async def update_agent_prompt(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    system_prompt = str((payload or {}).get('system_prompt') or '').strip()
    agent.system_prompt = system_prompt or None
    db.commit()
    db.refresh(agent)
    return {'ok': True, 'agent_id': str(agent.id), 'system_prompt': agent.system_prompt or ''}


@router.put('/agents/{agent_id}/function-profile')
async def bind_agent_function_profile(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    profile_id = str((payload or {}).get('function_profile_id') or '').strip()
    custom = None
    if isinstance(payload, dict):
        custom = payload.get('custom_functions_definition_template')
        if custom is None:
            custom = payload.get('custom_function_guide')
    if profile_id:
        profile = db.query(FunctionProfile).filter(FunctionProfile.id == profile_id, FunctionProfile.enabled == True).first()  # noqa: E712
        if not profile:
            raise validation_error('function_profile_id 無效或未啟用')

    cfg = agent.model_config if isinstance(agent.model_config, dict) else {}
    out = dict(cfg)
    out['function_profile_id'] = profile_id or None
    if isinstance(custom, str):
        out['functions_definition_template'] = custom
        out['toolcall_guide'] = custom
    agent.model_config = out
    agent.function_profile_id = profile_id or None
    flag_modified(agent, 'model_config')
    db.commit()
    db.refresh(agent)
    return {
        'ok': True,
        'agent_id': str(agent.id),
        'function_profile_id': out.get('function_profile_id'),
        'custom_functions_definition_template': out.get('functions_definition_template') if isinstance(out.get('functions_definition_template'), str) else (out.get('toolcall_guide') if isinstance(out.get('toolcall_guide'), str) else ''),
        'custom_function_guide': out.get('toolcall_guide') if isinstance(out.get('toolcall_guide'), str) else '',
    }


@router.post('/agents/{agent_id}/rag/datasets', deprecated=True)
async def create_agent_private_dataset(
    agent_id: str,
    response: Response,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if response is not None:
        _mark_agent_private_dataset_api_deprecated(response=response, operation='create', agent_id=agent_id)
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    name = str((payload or {}).get('name') or '').strip()
    if not name:
        raise validation_error('name 為必填')
    sensitivity = str((payload or {}).get('sensitivity') or 'normal').strip() or 'normal'
    if sensitivity not in {'normal', 'confidential', 'restricted'}:
        raise validation_error('sensitivity 僅允許 normal/confidential/restricted')
    row = RagDataset(
        name=name,
        scope='agent_private',
        agent_id=agent.id,
        owner_user_id=current_user.id,
        sensitivity=sensitivity,
        vector_backend=str((payload or {}).get('vector_backend') or '') or None,
        index_name=str((payload or {}).get('index_name') or '') or None,
        enabled=bool((payload or {}).get('enabled', True)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        'ok': True,
        'dataset': {'id': str(row.id), 'name': row.name, 'scope': row.scope, 'agent_id': str(row.agent_id)},
        'deprecated': True,
        'replacement': '/api/rag/datasets',
    }


@router.put('/agents/{agent_id}/rag/datasets/{dataset_id}', deprecated=True)
async def update_agent_private_dataset(
    agent_id: str,
    dataset_id: str,
    response: Response,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if response is not None:
        _mark_agent_private_dataset_api_deprecated(response=response, operation='update', agent_id=agent_id, dataset_id=dataset_id)
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
        uuid.UUID(str(dataset_id))
    except ValueError as error:
        raise not_found_error('RagDataset', dataset_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    dataset_row = db.query(RagDataset).filter(
        RagDataset.id == dataset_id,
        RagDataset.scope == 'agent_private',
        RagDataset.agent_id == agent.id,
    ).first()
    if dataset_row is None:
        raise not_found_error('RagDataset', dataset_id)

    if 'name' in (payload or {}):
        dataset_name = str((payload or {}).get('name') or '').strip()
        if not dataset_name:
            raise validation_error('name 不可為空')
        dataset_row.name = dataset_name

    if 'sensitivity' in (payload or {}):
        sensitivity = str((payload or {}).get('sensitivity') or '').strip()
        if sensitivity not in {'normal', 'confidential', 'restricted'}:
            raise validation_error('sensitivity 僅允許 normal/confidential/restricted')
        dataset_row.sensitivity = sensitivity

    if 'vector_backend' in (payload or {}):
        dataset_row.vector_backend = str((payload or {}).get('vector_backend') or '').strip() or None
    if 'index_name' in (payload or {}):
        dataset_row.index_name = str((payload or {}).get('index_name') or '').strip() or None
    if 'enabled' in (payload or {}):
        dataset_row.enabled = bool((payload or {}).get('enabled'))

    db.commit()
    db.refresh(dataset_row)
    return {
        'ok': True,
        'dataset': {
            'id': str(dataset_row.id),
            'name': dataset_row.name,
            'scope': dataset_row.scope,
            'agent_id': str(dataset_row.agent_id),
            'enabled': bool(dataset_row.enabled),
        },
        'deprecated': True,
        'replacement': '/api/rag/datasets/{dataset_id}',
    }


@router.delete('/agents/{agent_id}/rag/datasets/{dataset_id}', deprecated=True)
async def delete_agent_private_dataset(
    agent_id: str,
    dataset_id: str,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if response is not None:
        _mark_agent_private_dataset_api_deprecated(response=response, operation='delete', agent_id=agent_id, dataset_id=dataset_id)
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
        uuid.UUID(str(dataset_id))
    except ValueError as error:
        raise not_found_error('RagDataset', dataset_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    dataset_row = db.query(RagDataset).filter(
        RagDataset.id == dataset_id,
        RagDataset.scope == 'agent_private',
        RagDataset.agent_id == agent.id,
    ).first()
    if dataset_row is None:
        raise not_found_error('RagDataset', dataset_id)

    permission_keys_to_remove = _build_dataset_execute_permission_keys(dataset=dataset_row)
    db.delete(dataset_row)
    access_control_service.remove_permission_keys(db, sorted(permission_keys_to_remove))
    db.commit()
    return {'ok': True, 'id': str(dataset_id), 'deprecated': True, 'replacement': '/api/rag/datasets/{dataset_id}'}


@router.put('/agents/{agent_id}/rag/bindings')
async def bind_agent_rag_datasets(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：保留舊端點相容回應，不再執行 Agent 與資料集綁定。
    # 為什麼：資料集授權已改由 entity.dataset.* 控制，聊天檢索以請求 selected_dataset_ids 決定。
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)
    return {
        'ok': True,
        'deprecated': True,
        'agent_id': str(agent.id),
        'global_dataset_ids': [],
        'private_dataset_ids': [],
        'message': '已停用 Agent 資料集綁定；請改用 entity.dataset.* 權限與聊天 selected_dataset_ids。',
    }


@router.post('/agents/{agent_id}/mcp-test')
async def test_agent_mcp(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 使用 update_agent 權限作為最低門檻（設定頁測試）
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 目的：允許以 mcp_id 或 inline connection 兩種方式測試 MCP。
    # 為什麼：即使工具改為全域可用，管理頁仍需要便利的連線自檢入口。
    payload_obj = payload if isinstance(payload, dict) else {}
    conn = payload_obj.get('connection') if isinstance(payload_obj.get('connection'), dict) else None
    mcp_id = str(payload_obj.get('mcp_id') or '').strip()
    if conn is None and mcp_id:
        try:
            uuid.UUID(mcp_id)
        except Exception:
            return {'ok': False, 'error': 'mcp_id 格式不正確'}
        row = db.query(MCPConnection).filter(MCPConnection.id == mcp_id, MCPConnection.enabled == True).first()  # noqa: E712
        if not row:
            return {'ok': False, 'error': 'mcp_id 無效或未啟用'}
        conn = {
            'name': row.name,
            'transport': row.transport,
            'base_url': row.base_url,
            'auth': row.auth,
            'command': row.command,
            'args': row.args,
            'env': row.env,
        }

    if not isinstance(conn, dict):
        return {'ok': False, 'error': '缺少 connection 或 mcp_id'}

    name = (conn or {}).get('name') if isinstance(conn, dict) else None
    if not name or not isinstance(name, str):
        return {'ok': False, 'error': '缺少連線名稱'}

    transport = str((conn or {}).get('transport') or 'remote').strip().lower()
    if transport == 'stdio':
        command = str((conn or {}).get('command') or '').strip()
        if not command:
            return {'ok': False, 'error': '缺少 command'}
        return {'ok': True, 'transport': 'stdio', 'error': None}

    base_url = (conn or {}).get('base_url') if isinstance(conn, dict) else None
    if not base_url or not isinstance(base_url, str):
        return {'ok': False, 'error': '缺少 base_url'}

    # 先檢查 URL 格式
    try:
        from urllib.parse import urlparse
        u = urlparse(base_url)
        if not u.scheme or not u.netloc:
            return {'ok': False, 'error': 'base_url 格式不正確'}
    except Exception:
        return {'ok': False, 'error': 'base_url 格式不正確'}

    # 真實連線測試（httpx，短逾時，分類錯誤）
    client = MCPClient()
    auth = (conn or {}).get('auth') if isinstance(conn, dict) else None
    result = client.test_connection(base_url=base_url, auth=auth if isinstance(auth, dict) else None)
    return result


@router.post('/agents/{agent_id}/rag-test')
async def test_agent_rag(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    query = (payload or {}).get('query') if isinstance(payload, dict) else ''
    sources = (payload or {}).get('sources') if isinstance(payload, dict) else []
    topk = (payload or {}).get('topK') if isinstance(payload, dict) else 5
    if not isinstance(sources, list):
        return {'ok': False, 'results': [], 'error': 'sources 必須為陣列'}
    try:
        k = int(topk or 5)
        k = 5 if k <= 0 or k > 50 else k
    except Exception:
        k = 5
    collection_sources = [str(s).strip() for s in (sources or []) if isinstance(s, str) and str(s).strip()]

    default_collection = f"agent_{str(agent_id).replace('-', '')}_docs"
    if default_collection not in collection_sources:
        collection_sources.append(default_collection)

    if not query:
        return {'ok': True, 'results': [], 'collections': collection_sources}

    query_vector = embedding_service.embed_one(str(query))
    merged_results: list[dict] = []
    for collection in collection_sources:
        rows = qdrant_service.search(collection_name=collection, query_vector=query_vector, limit=k)
        for row in rows:
            payload_obj = row.get('payload') if isinstance(row, dict) and isinstance(row.get('payload'), dict) else {}
            merged_results.append({
                'docId': str(payload_obj.get('document_id') or row.get('id') or ''),
                'score': float(row.get('score') or 0.0),
                'snippet': str(payload_obj.get('snippet') or ''),
                'source': collection,
                'filename': str(payload_obj.get('filename') or ''),
            })

    merged_results.sort(key=lambda x: x.get('score', 0.0), reverse=True)
    return {'ok': True, 'results': merged_results[:k], 'collections': collection_sources}


@router.get('/agents')
async def list_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    agents = db.query(Agent).all()
    return {
        'agents': [
            {
                'id': str(a.id),
                'name': a.name,
                'description': a.description,
                'model_type': a.model_type,
                'agent_class': str(getattr(a, 'agent_class', 'tasked') or 'tasked'),
                'enabled': bool(getattr(a, 'enabled', True)),
                'is_router': bool(getattr(a, 'is_router', False)),
                'created_at': a.created_at.isoformat() if a.created_at else None,
                'updated_at': a.updated_at.isoformat() if a.updated_at else None,
            }
            for a in agents
        ]
    }


@router.post('/agents')
async def create_agent(
    name: str,
    description: str = '',
    model_type: str = 'cloud',
    agent_class: str = 'tasked',
    enabled: bool = True,
    model_config: dict = None,
    system_prompt: str = '',
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if model_config is None:
        model_config = {}
    if not check_permission(current_user, 'create_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    if not name or len(name.strip()) == 0:
        raise validation_error('Agent name is required')
    normalized_class = str(agent_class or 'tasked').strip().lower()
    if normalized_class not in {'master', 'public', 'tasked', 'private'}:
        raise validation_error('agent_class must be master/public/tasked/private')
    if normalized_class == 'master':
        enabled = True

    workspace = db.query(Workspace).first()
    if not workspace:
        workspace = Workspace(name='Default Workspace')
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

    agent = Agent(
        name=name,
        description=description,
        system_prompt=system_prompt.strip() or None,
        model_type=model_type,
        agent_class=normalized_class,
        enabled=bool(enabled),
        is_router=(normalized_class == 'master'),
        model_config=model_config,
        workspace_id=workspace.id,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'system_prompt': agent.system_prompt or '',
        'model_type': agent.model_type,
        'agent_class': str(getattr(agent, 'agent_class', 'tasked') or 'tasked'),
        'enabled': bool(getattr(agent, 'enabled', True)),
        'is_router': bool(getattr(agent, 'is_router', False)),
        'model_config': agent.model_config,
        'created_at': agent.created_at.isoformat() if agent.created_at else None,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.get('/agents/{agent_id}')
async def get_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    # Validate UUID format to avoid DB type conversion errors
    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'system_prompt': agent.system_prompt or '',
        'model_type': agent.model_type,
        'agent_class': str(getattr(agent, 'agent_class', 'tasked') or 'tasked'),
        'enabled': bool(getattr(agent, 'enabled', True)),
        'is_router': bool(getattr(agent, 'is_router', False)),
        'model_config': agent.model_config,
        'mcp_config': [],
        'skills': [],
        'tools': [],
        'rag_config': {},
        'workspace_id': str(agent.workspace_id),
        'created_at': agent.created_at.isoformat() if agent.created_at else None,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.put('/agents/{agent_id}')
async def update_agent(
    agent_id: str,
    name: str | None = None,
    description: str | None = None,
    model_type: str | None = None,
    agent_class: str | None = None,
    enabled: bool | None = None,
    model_config: dict | None = None,
    system_prompt: str | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 相容兩種呼叫方式：query params（舊）與 JSON body（前端）
    payload = None
    try:
        if request is not None:
            raw = await request.body()
            if raw:
                payload = await request.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        if 'name' in payload and name is None:
            v = payload.get('name')
            name = str(v) if isinstance(v, str) else name
        if 'description' in payload and description is None:
            v = payload.get('description')
            description = str(v) if isinstance(v, str) else description
        if 'model_type' in payload and model_type is None:
            v = payload.get('model_type')
            model_type = str(v) if isinstance(v, str) else model_type
        if 'model_config' in payload and model_config is None and isinstance(payload.get('model_config'), dict):
            model_config = payload.get('model_config')
        if 'agent_class' in payload and agent_class is None:
            v = payload.get('agent_class')
            agent_class = str(v) if isinstance(v, str) else agent_class
        if 'enabled' in payload and enabled is None:
            v = payload.get('enabled')
            if isinstance(v, bool):
                enabled = v
        if 'system_prompt' in payload and system_prompt is None:
            v = payload.get('system_prompt')
            system_prompt = str(v) if isinstance(v, str) else system_prompt

    if name is not None:
        agent.name = name
    if description is not None:
        agent.description = description
    if model_type is not None:
        mt = str(model_type).strip().lower()
        if mt not in {'cloud', 'local'}:
            raise validation_error('model_type must be cloud or local')
        agent.model_type = mt
    if agent_class is not None:
        normalized_class = str(agent_class).strip().lower()
        if normalized_class not in {'master', 'public', 'tasked', 'private'}:
            raise validation_error('agent_class must be master/public/tasked/private')
        agent.agent_class = normalized_class
        agent.is_router = normalized_class == 'master'
        if normalized_class == 'master':
            agent.enabled = True
    if enabled is not None:
        if str(getattr(agent, 'agent_class', '') or '') == 'master' and not bool(enabled):
            raise validation_error('master 代理不可停用')
        agent.enabled = bool(enabled)
    if model_config is not None:
        # 目的：更新代理基本資料時保留既有 LLM/整合設定，不被空 payload 覆蓋。
        # 為什麼：多個設定頁會分段更新 model_config，直接覆蓋會造成 LLM 設定被清空。
        if not isinstance(model_config, dict):
            raise validation_error('model_config must be an object')
        existing_config = agent.model_config if isinstance(agent.model_config, dict) else {}
        merged_config = dict(existing_config)
        for key, value in model_config.items():
            normalized_key = str(key)
            if value is None:
                merged_config.pop(normalized_key, None)
                continue
            merged_config[normalized_key] = value
        agent.model_config = merged_config
    if system_prompt is not None:
        agent.system_prompt = system_prompt.strip() or None

    db.commit()
    db.refresh(agent)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'model_type': agent.model_type,
        'system_prompt': agent.system_prompt or '',
        'agent_class': str(getattr(agent, 'agent_class', 'tasked') or 'tasked'),
        'enabled': bool(getattr(agent, 'enabled', True)),
        'is_router': bool(getattr(agent, 'is_router', False)),
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.delete('/agents/{agent_id}')
async def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'delete_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    db.delete(agent)
    db.commit()

    return None


@router.put('/agents/{agent_id}/llm-config')
async def update_agent_llm_config(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 允許前端傳 raw dict 或 {"model_config": {...}}
    model_config = payload.get('model_config', payload) if isinstance(payload, dict) else None
    if not isinstance(model_config, dict):
        raise validation_error('model_config must be an object')

    # 目的：只更新 LLM 設定欄位，不覆蓋其它 model_config 內容。
    # 為什麼：不同頁面分開編輯時需避免互相覆寫，維持設定穩定。
    existing_config = agent.model_config if isinstance(agent.model_config, dict) else {}
    merged_config = dict(existing_config)
    for key, value in model_config.items():
        if value is None:
            merged_config.pop(str(key), None)
            continue
        merged_config[str(key)] = value

    agent.model_config = merged_config
    db.commit()
    db.refresh(agent)

    return {
        'id': str(agent.id),
        'model_type': agent.model_type,
        'model_config': agent.model_config,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.post('/agents/{agent_id}/llm-test')
async def test_agent_llm(
    agent_id: str,
    overrides: dict | None = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 使用 chat 權限作為測試的最低門檻
    if not check_permission(current_user, 'chat', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 構建 overrides：以呼叫者提供的覆蓋優先，其次為 agent.model_config
    ov: dict = {}
    if isinstance(agent.model_config, dict):
        ov.update(agent.model_config)
    if isinstance(overrides, dict):
        ov.update(overrides)

    tier = ov.get('tier')
    router = ChatRouter()
    try:
        _ = router.single_turn(
            session_id=str(agent.id),
            agent_id=str(agent.id),
            user_message='ping',
            tier=tier,
            db=None,
            llm_overrides=ov,
        )
        info = getattr(router._llm, 'last_route_info', lambda: {})()  # type: ignore[attr-defined]
        return {
            'ok': True,
            'route': info,
        }
    except Exception as e:
        return {
            'ok': False,
            'error': str(e),
        }


@router.post('/agents/{agent_id}/llm-health')
async def health_agent_llm(
    agent_id: str,
    payload: dict | None = Body(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 使用 chat 權限作為測試的最低門檻
    if not check_permission(current_user, 'chat', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError as error:
        raise not_found_error('Agent', agent_id) from error

    overrides = (payload or {}).get('overrides') if isinstance(payload, dict) else None
    mode = (payload or {}).get('mode') if isinstance(payload, dict) else None

    llm = LLMClient()
    llm.init_for_session(session_id=f"health-{agent_id}", preferred_tier=(overrides or {}).get('tier'), overrides=overrides or {})
    try:
        res = llm.health_check(mode=(mode or 'soft'))
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}
