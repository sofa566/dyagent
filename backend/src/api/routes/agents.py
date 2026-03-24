from fastapi import APIRouter, Depends, Body, Request
import uuid
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from src.core.database import get_db
from src.models import User, Agent, Workspace, SkillEntry, FunctionProfile, RagDataset
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error
from src.services.chat_router import ChatRouter
from src.services.llm_client import LLMClient
from src.services.mcp_client import MCPClient
from src.services.qdrant_service import qdrant_service
from src.services.embedding_service import embedding_service

router = APIRouter()


@router.get('/agents/public')
async def list_public_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提供一般使用者可見的代理者清單（寬鬆模式）。

    - 僅需通過身份驗證；不再要求 `read_agent`/`chat` 權限，避免一般使用者 403。
    - 僅回傳基本資訊供前端選擇。
    """
    # 僅驗證登入；不做額外權限限制
    agents = db.query(Agent).all()
    return {
        'agents': [
            {
                'id': str(a.id),
                'name': a.name,
                'description': a.description,
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
    if not check_permission(current_user, 'read_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    def _default_mcp(value):
        # 正規化為 list 以簡化前端處理
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            # 若為物件，視為 map，取值為陣列
            return list(value.values())
        return []

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

    # 解析 skills 名單：優先 skill_ids，再合併 agent.skills 去重；同時提供 schema 摘要
    skills_out: list[str] = []
    skill_schemas: dict[str, dict] = {}
    try:
        cfg = agent.model_config if isinstance(agent.model_config, dict) else {}
        sids = list((cfg or {}).get('skill_ids') or []) if isinstance(cfg, dict) else []
        if sids:
            rows = db.query(SkillEntry).filter(SkillEntry.id.in_(sids)).all()
            for r in rows:
                if bool(r.enabled) and isinstance(r.name, str) and r.name.strip():
                    nm = r.name.strip()
                    if nm not in skills_out:
                        skills_out.append(nm)
                    try:
                        if isinstance(getattr(r, 'input_schema', None), dict):
                            skill_schemas[nm] = r.input_schema or {}
                    except Exception:
                        pass
        # 兼容：合併 agent.skills 中的字串
        for s in (agent.skills or []):
            if isinstance(s, str) and s.strip() and s not in skills_out:
                skills_out.append(s.strip())
        # 若有名稱但未從 id 抓到 schema，可再以名稱查一次（非嚴格）
        if skills_out:
            miss = [n for n in skills_out if n not in skill_schemas]
            if miss:
                rows2 = db.query(SkillEntry).filter(SkillEntry.name.in_(miss)).all()
                for r in rows2:
                    try:
                        if isinstance(getattr(r, 'input_schema', None), dict):
                            skill_schemas[r.name] = r.input_schema or {}
                    except Exception:
                        pass
    except Exception:
        skills_out = agent.skills or []
        skill_schemas = {}

    # 蒐集 MCP schemas：優先 model_config.mcp_ids 對應的全域設定，其次合併 inline mcp_config
    mcp_schemas: dict[str, dict] = {}
    try:
        from src.models import MCPConnection
        cfg2 = agent.model_config if isinstance(agent.model_config, dict) else {}
        mids = list((cfg2 or {}).get('mcp_ids') or []) if isinstance(cfg2, dict) else []
        if mids:
            rows = db.query(MCPConnection).filter(MCPConnection.id.in_(mids)).all()
            for r in rows:
                if bool(r.enabled) and isinstance(r.name, str) and r.name.strip():
                    mcp_schemas[f"mcp:{r.name.strip()}"] = getattr(r, 'input_schema', {}) or {}
        # inline 覆蓋
        mraw = agent.mcp_config or []
        if isinstance(mraw, dict):
            mlist = list(mraw.values())
        elif isinstance(mraw, list):
            mlist = mraw
        else:
            mlist = []
        for c in (mlist or []):
            if isinstance(c, dict) and c.get('name'):
                nm = str(c.get('name')).strip()
                sc = c.get('input_schema') if isinstance(c.get('input_schema'), dict) else {}
                if nm and sc:
                    mcp_schemas[f"mcp:{nm}"] = sc
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

    rag_dataset_ids = {'global_dataset_ids': [], 'private_dataset_ids': []}
    try:
        cfg3 = agent.rag_config if isinstance(agent.rag_config, dict) else {}
        rag_dataset_ids['global_dataset_ids'] = list(cfg3.get('global_dataset_ids') or [])
        rag_dataset_ids['private_dataset_ids'] = list(cfg3.get('private_dataset_ids') or [])
    except Exception:
        pass

    return {
        'mcp_config': _default_mcp(agent.mcp_config or {}),
        'skills': skills_out,
        'rag_config': agent.rag_config or {'enabled': False, 'sources': [], 'topK': 5},
        'functions_definition_template': tc_guide,
        'toolcall_guide': tc_guide,
        'model_provider': provider or '',
        'skill_examples': skill_examples,
        'skill_schemas': skill_schemas,
        'mcp_schemas': mcp_schemas,
        # 參照式設定（來自全域管理清單）
        'mcp_ids': (agent.model_config or {}).get('mcp_ids', []) if isinstance(agent.model_config, dict) else [],
        'skill_ids': (agent.model_config or {}).get('skill_ids', []) if isinstance(agent.model_config, dict) else [],
        'function_profile_id': function_profile_id,
        'rag_dataset_ids': rag_dataset_ids,
    }


@router.put('/agents/{agent_id}/integrations')
async def update_agent_integrations(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 讀取欄位並做基本驗證
    mcp_cfg = payload.get('mcp_config', []) if isinstance(payload, dict) else []
    skills = payload.get('skills', []) if isinstance(payload, dict) else []
    rag_cfg = payload.get('rag_config', {}) if isinstance(payload, dict) else {}
    function_profile_id = payload.get('function_profile_id') if isinstance(payload, dict) else None
    mcp_ids = payload.get('mcp_ids', []) if isinstance(payload, dict) else []
    skill_ids = payload.get('skill_ids', []) if isinstance(payload, dict) else []

    if mcp_cfg is not None and not isinstance(mcp_cfg, list):
        raise validation_error('mcp_config 必須為陣列')
    if skills is not None and not isinstance(skills, list):
        raise validation_error('skills 必須為陣列')
    if rag_cfg is not None and not isinstance(rag_cfg, dict):
        raise validation_error('rag_config 必須為物件')
    if function_profile_id is not None and not isinstance(function_profile_id, str):
        raise validation_error('function_profile_id 必須為字串')
    if mcp_ids is not None and not isinstance(mcp_ids, list):
        raise validation_error('mcp_ids 必須為陣列')
    if skill_ids is not None and not isinstance(skill_ids, list):
        raise validation_error('skill_ids 必須為陣列')

    # 基礎清理
    def _clean_skill(name: str) -> str:
        return (name or '').strip()[:64]

    skills_clean = []
    seen = set()
    for s in (skills or []):
        if not isinstance(s, str):
            continue
        n = _clean_skill(s)
        if n and n not in seen:
            seen.add(n)
            skills_clean.append(n)

    # rag 預設值
    rag_out = {
        'enabled': bool((rag_cfg or {}).get('enabled', False)),
        'sources': list((rag_cfg or {}).get('sources', []) or []),
        'topK': int((rag_cfg or {}).get('topK', 5) or 5),
        'global_dataset_ids': list((rag_cfg or {}).get('global_dataset_ids', []) or []),
        'private_dataset_ids': list((rag_cfg or {}).get('private_dataset_ids', []) or []),
    }
    if rag_out['topK'] < 1 or rag_out['topK'] > 50:
        raise validation_error('rag_config.topK 必須介於 1..50')

    if isinstance(function_profile_id, str) and function_profile_id.strip():
        profile = db.query(FunctionProfile).filter(FunctionProfile.id == function_profile_id.strip(), FunctionProfile.enabled == True).first()  # noqa: E712
        if not profile:
            raise validation_error('function_profile_id 無效或未啟用')

    agent.mcp_config = mcp_cfg or []
    agent.skills = skills_clean
    agent.rag_config = rag_out
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
        # 儲存參照式清單（保持彈性：允許同時存在 mcp_config 與 mcp_ids；由準備階段合併）
        if isinstance(mcp_ids, list):
            cfg['mcp_ids'] = [str(x) for x in mcp_ids if isinstance(x, (str,)) and x]
        if isinstance(skill_ids, list):
            cfg['skill_ids'] = [str(x) for x in skill_ids if isinstance(x, (str,)) and x]
        # 寫入 skills 的示例參數（限制於當前啟用/選取的 skills）
        sk_ex = (payload or {}).get('skill_examples') if isinstance(payload, dict) else None
        if isinstance(sk_ex, dict):
            # 僅保留在 skills_clean 內的鍵；值可為字串(JSON)或物件
            filtered = {}
            for k, v in sk_ex.items():
                if k in skills_clean and (isinstance(v, (str, dict))):
                    filtered[k] = v
            cfg['skill_examples'] = filtered
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
        log = Log(
            user_id=current_user.id,
            level='info',
            action='agent.integrations.update',
            resource_type='agent',
            resource_id=agent.id,
            details={'counts': {
                'mcp': len(agent.mcp_config or []),
                'skills': len(agent.skills or []),
                'sources': len(rag_out['sources']),
                'mcp_ids': len((agent.model_config or {}).get('mcp_ids', []) if isinstance(agent.model_config, dict) else []),
                'skill_ids': len((agent.model_config or {}).get('skill_ids', []) if isinstance(agent.model_config, dict) else []),
            }},
            ip_address=None,
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()

    return {
        'mcp_config': agent.mcp_config or [],
        'skills': agent.skills or [],
        'rag_config': agent.rag_config or {'enabled': False, 'sources': [], 'topK': 5},
        'functions_definition_template': ((agent.model_config or {}).get('functions_definition_template') if isinstance(agent.model_config, dict) else None) or ((agent.model_config or {}).get('toolcall_guide') if isinstance(agent.model_config, dict) else ''),
        'toolcall_guide': ((agent.model_config or {}).get('functions_definition_template') if isinstance(agent.model_config, dict) else None) or ((agent.model_config or {}).get('toolcall_guide') if isinstance(agent.model_config, dict) else ''),
        'mcp_ids': (agent.model_config or {}).get('mcp_ids', []) if isinstance(agent.model_config, dict) else [],
        'skill_ids': (agent.model_config or {}).get('skill_ids', []) if isinstance(agent.model_config, dict) else [],
        'function_profile_id': (agent.model_config or {}).get('function_profile_id') if isinstance(agent.model_config, dict) else None,
    }


@router.get('/agents/{agent_id}/prompt')
async def get_agent_prompt(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)
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
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)
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
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)
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


@router.post('/agents/{agent_id}/rag/datasets')
async def create_agent_private_dataset(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)
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
    return {'ok': True, 'dataset': {'id': str(row.id), 'name': row.name, 'scope': row.scope, 'agent_id': str(row.agent_id)}}


@router.put('/agents/{agent_id}/rag/bindings')
async def bind_agent_rag_datasets(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    global_ids = [str(x) for x in list((payload or {}).get('global_dataset_ids') or []) if x]
    private_ids = [str(x) for x in list((payload or {}).get('private_dataset_ids') or []) if x]

    if global_ids:
        rows = db.query(RagDataset).filter(RagDataset.id.in_(global_ids)).all()
        if len(rows) != len(set(global_ids)):
            raise validation_error('global_dataset_ids 含無效 id')
        for r in rows:
            if r.scope != 'global':
                raise validation_error('global_dataset_ids 只能綁定 scope=global')
    if private_ids:
        rows = db.query(RagDataset).filter(RagDataset.id.in_(private_ids)).all()
        if len(rows) != len(set(private_ids)):
            raise validation_error('private_dataset_ids 含無效 id')
        for r in rows:
            if r.scope != 'agent_private' or str(r.agent_id) != str(agent.id):
                raise validation_error('private_dataset_ids 僅能綁定本代理者私有資料集')

    rag_cfg = agent.rag_config if isinstance(agent.rag_config, dict) else {}
    out = dict(rag_cfg)
    out['global_dataset_ids'] = global_ids
    out['private_dataset_ids'] = private_ids
    agent.rag_config = out
    flag_modified(agent, 'rag_config')
    db.commit()
    db.refresh(agent)
    return {'ok': True, 'agent_id': str(agent.id), 'global_dataset_ids': global_ids, 'private_dataset_ids': private_ids}


@router.post('/agents/{agent_id}/mcp-test')
async def test_agent_mcp(
    agent_id: str,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 使用 update_agent 權限作為最低門檻（設定頁測試）
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    conn = (payload or {}).get('connection') if isinstance(payload, dict) else None
    name = (conn or {}).get('name') if isinstance(conn, dict) else None
    base_url = (conn or {}).get('base_url') if isinstance(conn, dict) else None
    if not name or not isinstance(name, str):
        return {'ok': False, 'error': '缺少連線名稱'}
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
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

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
    rag_cfg = agent.rag_config if isinstance(agent.rag_config, dict) else {}
    cfg_sources = list((rag_cfg or {}).get('sources', []) or [])
    collection_sources = [str(s).strip() for s in (sources or []) if isinstance(s, str) and str(s).strip()]
    if not collection_sources:
        collection_sources = [str(s).strip() for s in cfg_sources if isinstance(s, str) and str(s).strip()]

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
    if not check_permission(current_user, 'read_agent'):
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
    model_config: dict = {},
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'create_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    if not name or len(name.strip()) == 0:
        raise validation_error('Agent name is required')

    workspace = db.query(Workspace).first()
    if not workspace:
        workspace = Workspace(name='Default Workspace')
        db.add(workspace)
        db.commit()
        db.refresh(workspace)

    agent = Agent(
        name=name,
        description=description,
        model_type=model_type,
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
        'model_type': agent.model_type,
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
    if not check_permission(current_user, 'read_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    # Validate UUID format to avoid DB type conversion errors
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'model_type': agent.model_type,
        'model_config': agent.model_config,
        'mcp_config': agent.mcp_config,
        'skills': agent.skills,
        'tools': agent.tools,
        'rag_config': agent.rag_config,
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
    model_config: dict | None = None,
    request: Request = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

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

    if name is not None:
        agent.name = name
    if description is not None:
        agent.description = description
    if model_type is not None:
        mt = str(model_type).strip().lower()
        if mt not in {'cloud', 'local'}:
            raise validation_error('model_type must be cloud or local')
        agent.model_type = mt
    if model_config is not None:
        agent.model_config = model_config

    db.commit()
    db.refresh(agent)

    return {
        'id': str(agent.id),
        'name': agent.name,
        'description': agent.description,
        'model_type': agent.model_type,
        'updated_at': agent.updated_at.isoformat() if agent.updated_at else None,
    }


@router.delete('/agents/{agent_id}')
async def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'delete_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

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
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # 允許前端傳 raw dict 或 {"model_config": {...}}
    model_config = payload.get('model_config', payload) if isinstance(payload, dict) else None
    if not isinstance(model_config, dict):
        raise validation_error('model_config must be an object')

    agent.model_config = model_config
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
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

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
    current_user: User = Depends(get_current_user),
):
    # 使用 chat 權限作為測試的最低門檻
    if not check_permission(current_user, 'chat'):
        from src.api.errors import forbidden_error
        raise forbidden_error()

    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    overrides = (payload or {}).get('overrides') if isinstance(payload, dict) else None
    mode = (payload or {}).get('mode') if isinstance(payload, dict) else None

    llm = LLMClient()
    llm.init_for_session(session_id=f"health-{agent_id}", preferred_tier=(overrides or {}).get('tier'), overrides=overrides or {})
    try:
        res = llm.health_check(mode=(mode or 'soft'))
        return res
    except Exception as e:
        return {"ok": False, "error": str(e)}
