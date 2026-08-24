from fastapi import APIRouter, Depends, Body, UploadFile, File
from typing import Any
from sqlalchemy.orm import Session
import zipfile
import io
from pathlib import Path
import re
import yaml
import shlex
import subprocess
import os

from src.core.database import get_db
from src.models import SkillEntry, SkillInteraction, User, Log, Agent
from src.middleware.auth import get_current_user
from src.middleware.rbac import require_permission
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error
from src.services.skill_executor import execute_skill
from src.services.llm_client import LLMClient


router = APIRouter()


def _build_master_agent_llm_overrides(db: Session) -> dict[str, Any]:
    """目的：組合主代理的 LLM 覆寫設定。
    為什麼：技能測試若未指定模型，需沿用主代理預設路由，避免測試階段出現模型未連接。
    """
    master_agent = db.query(Agent).filter(Agent.is_router == True, Agent.enabled == True).first()  # noqa: E712
    if master_agent is None:
        return {}

    overrides: dict[str, Any] = {}
    cfg = master_agent.model_config if isinstance(master_agent.model_config, dict) else {}
    try:
        model_type = str(getattr(master_agent, 'model_type', '') or '')
        if model_type == 'local' and not bool(cfg.get('tier')):
            overrides['tier'] = 'onprem'
        if model_type == 'cloud' and not bool(cfg.get('tier')):
            overrides['tier'] = 'cloud'
        for key in (
            'tier', 'provider', 'model', 'base_url', 'onprem_provider', 'onprem_base_url', 'api_key', 'api_key_ref',
            'azure_endpoint', 'azure_api_version', 'azure_deployment', 'azure_api_key_ref',
        ):
            value = cfg.get(key)
            if value:
                overrides[key] = value
        if not overrides.get('onprem_base_url') and isinstance(cfg.get('onprem'), dict):
            value = cfg.get('onprem', {}).get('base_url')
            if value:
                overrides['onprem_base_url'] = value
        if not overrides.get('provider') and isinstance(cfg.get('cloud'), dict):
            value = cfg.get('cloud', {}).get('provider')
            if value:
                overrides['provider'] = value
        if not overrides.get('model') and isinstance(cfg.get('cloud'), dict):
            value = cfg.get('cloud', {}).get('model')
            if value:
                overrides['model'] = value
    except Exception:
        return {}

    return overrides


def _to_dict(s: SkillEntry) -> dict[str, Any]:
    prompt_text = str(getattr(s, 'prompt_template', '') or '').strip()
    has_zip = bool(getattr(s, 'zip_bundle', None))
    skill_type = str(getattr(s, 'skill_type', '') or '').strip()
    if not skill_type:
        skill_type = 'webhook' if str(getattr(s, 'type', 'webhook') or 'webhook') == 'webhook' and not prompt_text and not has_zip else 'executable'
    return {
        'id': str(s.id),
        'name': s.name,
        'description': s.description or '',
        'enabled': bool(s.enabled),
        'type': s.type,
        'endpoint_url': s.endpoint_url or '',
        'http_method': s.http_method or 'POST',
        'headers': s.headers or {},
        'timeout_ms': s.timeout_ms or 8000,
        'python_handler': s.python_handler or '',
        'command': s.command or '',
        'input_schema': s.input_schema or {},
        # Claude Skills 相容欄位
        'skill_type': skill_type,
        'prompt_template': s.prompt_template or '',
        'has_zip': has_zip,  # 不回傳整個 ZIP，僅回傳是否存在
        'references': s.references,
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'updated_at': s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get('/skills')
@require_permission('skills.read')
async def list_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.query(SkillEntry).order_by(SkillEntry.created_at.desc()).all()
    return {'skills': [_to_dict(r) for r in rows]}


@router.get('/skills/selectable')
async def list_selectable_skills(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提供可被代理者綁定的 Skill 清單（admin / agent_admin 可用）。"""
    if not check_permission(current_user, 'update_agent', db=db):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    rows = db.query(SkillEntry).filter(SkillEntry.enabled == True).order_by(SkillEntry.created_at.desc()).all()  # noqa: E712
    return {'skills': [_to_dict(r) for r in rows]}


# ========== Claude Skills ZIP 匯入 ==========

def _parse_skill_md(content: str) -> dict[str, str]:
    """解析 SKILL.md 內容（YAML front matter 或純 Markdown）。"""
    content = (content or '').strip()
    if not content:
        return {'name': '', 'description': '', 'prompt_template': '', 'raw_content': ''}

    # 嘗試解析 YAML front matter
    yaml_match = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)', content, re.DOTALL)
    if yaml_match:
        try:
            meta = yaml.safe_load(yaml_match.group(1)) or {}
            body = yaml_match.group(2).strip()
            return {
                'name': str(meta.get('name') or '').strip(),
                'description': str(meta.get('description') or '').strip(),
                'prompt_template': str(meta.get('prompt') or body).strip(),
                'raw_content': content,
            }
        except yaml.YAMLError:
            pass

    # 純 Markdown 格式
    lines = content.split('\n')
    name = ''
    start_idx = 0
    if lines and lines[0].startswith('# '):
        name = lines[0][2:].strip()
        start_idx = 1
    prompt_template = '\n'.join(lines[start_idx:]).strip()
    return {'name': name, 'description': '', 'prompt_template': prompt_template, 'raw_content': content}


def _parse_claude_skill_zip(content: bytes) -> dict[str, Any]:
    """解析 Claude Skill ZIP 檔案。

    ZIP 結構範例：
        skill-name/
        ├── SKILL.md           # 必需：提示詞模板
        ├── scripts/           # 選用：可執行腳本
        │   └── process.py
        └── references/        # 選用：參考文檔
            └── guide.md
    """
    result: dict[str, Any] = {
        'skill_md': None,
        'scripts': [],
        'references': []
    }

    try:
        with zipfile.ZipFile(io.BytesIO(content), 'r') as zf:
            for name in zf.namelist():
                if name.endswith('/'):
                    continue

                path = Path(name)
                parts = path.parts

                # 找 SKILL.md
                if path.name.upper() == 'SKILL.MD':
                    text = zf.read(name).decode('utf-8')
                    result['skill_md'] = _parse_skill_md(text)

                # 找 scripts/ 下的腳本
                elif 'scripts' in parts:
                    try:
                        text = zf.read(name).decode('utf-8')
                        ext = path.suffix.lower()
                        if ext == '.py':
                            script_type = 'python'
                        elif ext in {'.js', '.mjs'}:
                            script_type = 'nodejs'
                        elif ext == '.sh':
                            script_type = 'bash'
                        else:
                            script_type = 'other'
                        result['scripts'].append({
                            'filename': path.name,
                            'type': script_type,
                            'code': text
                        })
                    except UnicodeDecodeError:
                        pass

                # 找 references/ 下的文件
                elif 'references' in parts and path.suffix.lower() in ('.md', '.txt'):
                    try:
                        text = zf.read(name).decode('utf-8')
                        result['references'].append({
                            'filename': path.name,
                            'content': text
                        })
                    except UnicodeDecodeError:
                        pass
    except zipfile.BadZipFile:
        pass

    return result


def _normalize_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ''


def _requires_executable(*, skill_type: str, prompt_template: str, has_zip_bundle: bool) -> bool:
    if skill_type == 'executable':
        return True
    if skill_type == 'hybrid':
        return not (bool(prompt_template.strip()) or bool(has_zip_bundle))
    return False


def _resolve_executable_mode(*, command: str, endpoint_url: str, python_handler: str) -> str:
    if command:
        return 'command'
    if endpoint_url:
        return 'webhook'
    if python_handler:
        return 'python'
    return ''


def _run_command(command: str, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """目的：執行 executable command 並回傳結構化結果。
    為什麼：讓 executable 技能可透過 uvx/npx/java 等命令運行，而非僅限 python handler。
    """
    cmd_text = _normalize_text(command)
    if not cmd_text:
        return {'ok': False, 'error': 'empty_command'}
    try:
        args = shlex.split(cmd_text)
    except Exception as error:
        return {'ok': False, 'error': f'invalid_command: {error}'}
    if not args:
        return {'ok': False, 'error': 'empty_command'}

    allowed_prefixes = {'uvx', 'npx', 'node', 'java', 'python', 'python3'}
    if args[0] not in allowed_prefixes:
        return {'ok': False, 'error': f'command_not_allowed: {args[0]}'}

    env = os.environ.copy()
    import json as _json
    env['SKILL_INPUT'] = _json.dumps(payload or {}, ensure_ascii=False)
    timeout_seconds = max(1, int((timeout_ms or 8000) / 1000))
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': f'command_timeout_{timeout_seconds}s'}
    except Exception as error:
        return {'ok': False, 'error': str(error)}

    stdout = str(proc.stdout or '').strip()
    stderr = str(proc.stderr or '').strip()
    if proc.returncode != 0:
        return {
            'ok': False,
            'error': f'command_exit_{proc.returncode}',
            'stdout': stdout[:2000],
            'stderr': stderr[:2000],
        }

    if not stdout:
        return {'ok': True, 'result': {'text': '', 'stdout': '', 'return_code': 0}}

    try:
        data = _json.loads(stdout)
        return {'ok': True, 'result': data}
    except Exception:
        return {'ok': True, 'result': {'text': stdout[:6000], 'return_code': 0}}


@router.post('/skills/import-claude-skill-zip')
@require_permission('skills.create')
async def import_claude_skill_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上傳並解析 Claude Skill ZIP 檔案。

    Request: multipart/form-data
        file: ZIP 檔案

    Response:
        ok: True
        parsed: { skill_md, scripts, references }
    """
    if not file.filename or not file.filename.lower().endswith('.zip'):
        raise validation_error('請上傳 .zip 檔案')

    content = await file.read()
    if not content:
        raise validation_error('檔案內容為空')

    parsed = _parse_claude_skill_zip(content)

    if not parsed.get('skill_md') or not parsed['skill_md'].get('prompt_template'):
        raise validation_error('ZIP 中未找到有效的 SKILL.md')

    return {'ok': True, 'parsed': parsed, 'zip_size': len(content)}


@router.post('/skills/import-claude-skill-zip/create')
@require_permission('skills.create')
async def create_from_claude_skill_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上傳 Claude Skill ZIP 並直接建立 SkillEntry。

    ZIP 內容會儲存到 zip_bundle 欄位，執行時再解壓。

    Request: multipart/form-data
        file: ZIP 檔案

    Response:
        ok: True
        skill: 建立的 SkillEntry
    """
    if not file.filename or not file.filename.lower().endswith('.zip'):
        raise validation_error('請上傳 .zip 檔案')

    content = await file.read()
    if not content:
        raise validation_error('檔案內容為空')

    parsed = _parse_claude_skill_zip(content)
    skill_md = parsed.get('skill_md')

    if not skill_md or not skill_md.get('prompt_template'):
        raise validation_error('ZIP 中未找到有效的 SKILL.md')

    name = str(skill_md.get('name') or 'imported-skill').strip()

    # 檢查名稱是否已存在
    exists = db.query(SkillEntry).filter(SkillEntry.name == name).first()
    if exists:
        base_name = name
        for i in range(2, 100):
            name = f"{base_name}-{i}"
            if not db.query(SkillEntry).filter(SkillEntry.name == name).first():
                break

    # 決定 skill_type
    has_scripts = bool(parsed.get('scripts'))
    skill_type = 'hybrid' if has_scripts else 'prompt'

    row = SkillEntry(
        name=name,
        description=str(skill_md.get('description') or ''),
        enabled=True,
        type='python',  # 舊版欄位，設為 python（將由 skill_executor 處理）
        skill_type=skill_type,
        prompt_template=str(skill_md.get('raw_content') or skill_md.get('prompt_template') or ''),
        zip_bundle=content,  # 儲存完整 ZIP
        references=parsed.get('references') or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {'ok': True, 'skill': _to_dict(row)}


@router.post('/skills')
@require_permission('skills.create')
async def create_skill(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    name = (payload or {}).get('name') or ''
    if not isinstance(name, str) or not name.strip():
        raise validation_error('name 為必填')
    exists = db.query(SkillEntry).filter(SkillEntry.name == name.strip()).first()
    if exists:
        raise validation_error('名稱已存在')
    endpoint_url = (payload or {}).get('endpoint_url') or None
    http_method = ((payload or {}).get('http_method') or 'POST').upper()
    headers = (payload or {}).get('headers') or {}
    timeout_ms = (payload or {}).get('timeout_ms') or 8000
    python_handler = (payload or {}).get('python_handler') or None
    command = (payload or {}).get('command') or None
    input_schema = (payload or {}).get('input_schema') or {}

    # Claude Skills 相容欄位
    skill_type = str((payload or {}).get('skill_type') or 'executable').strip()
    if skill_type not in ('prompt', 'executable', 'hybrid', 'webhook'):
        skill_type = 'executable'
    prompt_template = (payload or {}).get('prompt_template') or None
    references = (payload or {}).get('references') or None
    prompt_text = _normalize_text(prompt_template)
    requires_executable = _requires_executable(skill_type=skill_type, prompt_template=prompt_text, has_zip_bundle=False)

    endpoint_text = _normalize_text(endpoint_url)
    python_handler_text = _normalize_text(python_handler)
    command_text = _normalize_text(command)
    executable_mode = _resolve_executable_mode(
        command=command_text,
        endpoint_url=endpoint_text,
        python_handler=python_handler_text,
    )
    if requires_executable and not executable_mode:
        raise validation_error('executable 類技能需提供 command 或 endpoint_url 或 python_handler')
    if skill_type == 'webhook' and not endpoint_text:
        raise validation_error('webhook 類技能需提供 endpoint_url')

    if executable_mode == 'webhook' and not endpoint_text:
        raise validation_error('webhook 類型需要 endpoint_url')
    if executable_mode == 'python' and not python_handler_text:
        raise validation_error('python 類型需要 python_handler')
    if not isinstance(headers, dict):
        raise validation_error('headers 必須為 JSON 物件')
    try:
        timeout_ms = int(timeout_ms)
    except Exception:
        timeout_ms = 8000
    if timeout_ms <= 0:
        timeout_ms = 8000

    if skill_type == 'webhook':
        resolved_type = 'webhook'
    elif executable_mode == 'webhook':
        resolved_type = 'webhook'
    elif executable_mode in {'python', 'command'}:
        resolved_type = 'python'
    else:
        resolved_type = 'webhook'

    s = SkillEntry(
        name=name.strip(),
        description=(payload or {}).get('description') or '',
        enabled=bool((payload or {}).get('enabled', True)),
        type=resolved_type,
        endpoint_url=endpoint_text or None,
        http_method=http_method,
        headers=headers,
        timeout_ms=timeout_ms,
        python_handler=python_handler_text or None,
        command=command_text or None,
        input_schema=input_schema if isinstance(input_schema, dict) else {},
        # Claude Skills 欄位
        skill_type=skill_type,
        prompt_template=prompt_template,
        references=references if isinstance(references, list) else None,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _to_dict(s)


@router.get('/skills/{skill_id}')
@require_permission('skills.read')
async def get_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)
    return _to_dict(row)


@router.put('/skills/{skill_id}')
@require_permission('skills.update')
async def update_skill(
    skill_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)
    if 'name' in (payload or {}):
        n = (payload or {}).get('name') or ''
        if not isinstance(n, str) or not n.strip():
            raise validation_error('name 不可為空')
        dup = db.query(SkillEntry).filter(SkillEntry.name == n.strip(), SkillEntry.id != row.id).first()
        if dup:
            raise validation_error('名稱已存在')
        row.name = n.strip()
    for k in ('description','enabled','type','endpoint_url','http_method','headers','timeout_ms','python_handler','command','input_schema',
               'skill_type','prompt_template','references'):
        if k in (payload or {}):
            val = (payload or {}).get(k)
            # skill_type 驗證
            if k == 'skill_type' and val not in ('prompt', 'executable', 'hybrid', 'webhook', None):
                val = 'executable'
            setattr(row, k, val)
    prompt_text = str(getattr(row, 'prompt_template', '') or '').strip()
    has_claude_prompt_or_zip = bool(prompt_text) or bool(getattr(row, 'zip_bundle', None))
    requires_executable = _requires_executable(skill_type=str(row.skill_type or 'executable'), prompt_template=prompt_text, has_zip_bundle=bool(getattr(row, 'zip_bundle', None)))

    command_text = _normalize_text(getattr(row, 'command', None))
    endpoint_text = _normalize_text(getattr(row, 'endpoint_url', None))
    handler_text = _normalize_text(getattr(row, 'python_handler', None))
    if str(getattr(row, 'type', 'webhook') or 'webhook') not in {'webhook', 'python'}:
        row.type = 'webhook'
    executable_mode = _resolve_executable_mode(command=command_text, endpoint_url=endpoint_text, python_handler=handler_text)

    if requires_executable and not executable_mode:
        raise validation_error('executable 類技能需提供 command 或 endpoint_url 或 python_handler')
    if str(row.skill_type or 'executable') == 'webhook' and not endpoint_text:
        raise validation_error('webhook 類技能需提供 endpoint_url')

    if executable_mode == 'webhook':
        row.type = 'webhook'
        row.endpoint_url = endpoint_text
        row.http_method = (row.http_method or 'POST').upper()
        if not isinstance(row.headers, dict):
            row.headers = {}
        try:
            row.timeout_ms = int(row.timeout_ms or 8000)
        except Exception:
            row.timeout_ms = 8000
        if row.timeout_ms <= 0:
            row.timeout_ms = 8000
    elif executable_mode in {'python', 'command'}:
        row.type = 'python'
        if executable_mode == 'python' and not handler_text:
            raise validation_error('python 類型需要 python_handler')
        row.python_handler = handler_text or None
        row.command = command_text or None

    if executable_mode != 'webhook' and not endpoint_text:
        row.endpoint_url = None
    if row.input_schema is not None and not isinstance(row.input_schema, dict):
        row.input_schema = {}
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.post('/skills/{skill_id}/test')
@require_permission('skills.update')
async def test_skill(
    skill_id: str,
    payload: dict[str, Any] = Body(default={}),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """執行單一技能測試：
    - webhook：依設定發送 HTTP 請求
    - python：importlib 載入 handler 並呼叫
    僅供管理介面測試使用。
    """
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)
    if not bool(row.enabled):
        return {'ok': False, 'error': 'skill_disabled'}

    # 先行以 input_schema 驗證輸入（若提供）
    def _extract_errors(exc: Exception):
        try:
            # fastjsonschema.JsonSchemaException: may have path property
            p = getattr(exc, 'path', None)
            msg = str(exc)
            if p is not None:
                if isinstance(p, (list, tuple)):
                    return [{'path': '.'.join(map(str, p)), 'message': msg}]
                return [{'path': str(p), 'message': msg}]
            # jsonschema.ValidationError: has .path (deque)
            from collections.abc import Iterable as _It
            path = getattr(exc, 'path', None)
            if path is not None and hasattr(path, '__iter__'):
                parts = [str(x) for x in list(path)]
                return [{'path': '.'.join(parts), 'message': msg}]
        except Exception:
            pass
        return [{'path': '', 'message': str(exc)}]

    try:
        schema = getattr(row, 'input_schema', None)
        if isinstance(schema, dict) and schema:
            try:
                try:
                    import fastjsonschema  # type: ignore
                    validator = fastjsonschema.compile(schema)
                    validator(payload or {})
                except ImportError:
                    import jsonschema  # type: ignore
                    jsonschema.validate(instance=payload or {}, schema=schema)
            except Exception as ve:
                return {'ok': False, 'error': 'schema_validation_failed', 'errors': _extract_errors(ve)}
    except Exception:
        pass

    import time as _time
    t0 = _time.time()
    skill_type = str(getattr(row, 'skill_type', 'executable') or 'executable').strip()
    has_zip_bundle = bool(getattr(row, 'zip_bundle', None))
    should_use_claude_skill = skill_type in {'prompt', 'hybrid'} or has_zip_bundle
    if should_use_claude_skill:
        exec_result = execute_skill(
            skill_id=str(getattr(row, 'id', '') or skill_id),
            zip_bundle=getattr(row, 'zip_bundle', None),
            prompt_template=str(getattr(row, 'prompt_template', '') or ''),
            input_data=payload or {},
            execute_scripts=True,
        )
        if not exec_result.ok:
            out = {'ok': False, 'error': str(exec_result.error or 'skill_zip_execution_failed')}
            try:
                lg = Log(
                    user_id=current_user.id,
                    level='error',
                    action='skill.test',
                    resource_type='skill',
                    resource_id=row.id,
                    details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'claude_skill', 'error': out['error'][:200]},
                    ip_address=None,
                )
                db.add(lg)
                db.commit()
            except Exception:
                db.rollback()
            return out

        failed_scripts = [x for x in (exec_result.script_outputs or []) if not bool(x.get('ok'))]
        if failed_scripts:
            out = {
                'ok': False,
                'error': f"script_execution_failed: {failed_scripts[0].get('script') or ''}".strip(),
                'script_outputs': exec_result.script_outputs or [],
            }
            try:
                lg = Log(
                    user_id=current_user.id,
                    level='error',
                    action='skill.test',
                    resource_type='skill',
                    resource_id=row.id,
                    details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'claude_skill', 'error': out['error'][:200]},
                    ip_address=None,
                )
                db.add(lg)
                db.commit()
            except Exception:
                db.rollback()
            return out

        merged_prompt = str(exec_result.prompt or '').strip()
        if not merged_prompt:
            out = {'ok': False, 'error': 'empty_prompt_template'}
            try:
                lg = Log(
                    user_id=current_user.id,
                    level='error',
                    action='skill.test',
                    resource_type='skill',
                    resource_id=row.id,
                    details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'claude_skill', 'error': out['error']},
                    ip_address=None,
                )
                db.add(lg)
                db.commit()
            except Exception:
                db.rollback()
            return out

        try:
            import json as _json
            prompt_payload = payload or {}
            if isinstance(prompt_payload, dict):
                prompt_payload = {k: v for k, v in prompt_payload.items() if k not in {'_script', '_args'}}
            payload_json = _json.dumps(prompt_payload or {}, ensure_ascii=False)
            full_prompt = (
                f"{merged_prompt}\n\n"
                "[技能輸入(JSON)]\n"
                f"{payload_json}\n\n"
                "請嚴格依模板要求輸出最終結果。"
            )
            llm = LLMClient()
            llm_overrides = _build_master_agent_llm_overrides(db)
            try:
                llm.init_for_session(
                    session_id=f"skill-test-{skill_id}",
                    preferred_tier=llm_overrides.get('tier'),
                    overrides=llm_overrides,
                )
            except TypeError:
                llm.init_for_session(
                    session_id=f"skill-test-{skill_id}",
                    preferred_tier=llm_overrides.get('tier'),
                )
            output_text = llm.complete(prompt=full_prompt, tier=None)
            route_info = llm.last_route_info()
            out = {
                'ok': True,
                'result': {
                    'mode': 'claude_skill',
                    'text': str(output_text or ''),
                    'script_outputs': exec_result.script_outputs or [],
                    'prompt_preview': merged_prompt,
                    'llm_route': route_info,
                },
            }
            try:
                lg = Log(
                    user_id=current_user.id,
                    level='info',
                    action='skill.test',
                    resource_type='skill',
                    resource_id=row.id,
                    details={'ok': True, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'claude_skill'},
                    ip_address=None,
                )
                db.add(lg)
                db.commit()
            except Exception:
                db.rollback()
            return out
        except Exception as e:
            out = {'ok': False, 'error': str(e), 'script_outputs': exec_result.script_outputs or []}
            try:
                lg = Log(
                    user_id=current_user.id,
                    level='error',
                    action='skill.test',
                    resource_type='skill',
                    resource_id=row.id,
                    details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'claude_skill', 'error': str(e)[:200]},
                    ip_address=None,
                )
                db.add(lg)
                db.commit()
            except Exception:
                db.rollback()
            return out

    try:
        timeout_ms = int(getattr(row, 'timeout_ms', 8000) or 8000)
    except Exception:
        timeout_ms = 8000

    command_text = _normalize_text(getattr(row, 'command', None))
    if command_text:
        result = _run_command(command_text, payload or {}, timeout_ms)
        details_type = 'command'
        try:
            lg = Log(
                user_id=current_user.id,
                level='info' if result.get('ok') else 'error',
                action='skill.test',
                resource_type='skill',
                resource_id=row.id,
                details={
                    'ok': bool(result.get('ok')),
                    'duration_ms': int((_time.time()-t0)*1000),
                    'type': details_type,
                    'error': str(result.get('error') or '')[:200],
                },
                ip_address=None,
            )
            db.add(lg)
            db.commit()
        except Exception:
            db.rollback()
        return result

    typ = str(row.type)
    if typ == 'python':
        handler = str(getattr(row, 'python_handler', '') or '').strip()
        if not handler or ':' not in handler:
            return {'ok': False, 'error': 'invalid_python_handler'}
        mod_name, func_name = handler.split(':', 1)
        try:
            import importlib
            mod = importlib.import_module(mod_name)
            func = getattr(mod, func_name)
            res = func(payload or {})
            if not isinstance(res, dict):
                res = {'ok': True, 'result': res}
            out = {'ok': True, 'result': res}
            # 寫入 Log
            try:
                lg = Log(
                    user_id=current_user.id,
                    level='info',
                    action='skill.test',
                    resource_type='skill',
                    resource_id=row.id,
                    details={'ok': True, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'python'},
                    ip_address=None,
                )
                db.add(lg); db.commit()
            except Exception:
                db.rollback()
            return out
        except Exception as e:
            out = {'ok': False, 'error': str(e)}
            try:
                lg = Log(
                    user_id=current_user.id,
                    level='error',
                    action='skill.test',
                    resource_type='skill',
                    resource_id=row.id,
                    details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'python', 'error': str(e)[:200]},
                    ip_address=None,
                ); db.add(lg); db.commit()
            except Exception:
                db.rollback()
            return out
    else:
        url = str(getattr(row, 'endpoint_url', '') or '').strip()
        method = str(getattr(row, 'http_method', 'POST') or 'POST').upper()
        headers = getattr(row, 'headers', {}) or {}
        if not url:
            return {'ok': False, 'error': 'invalid_webhook_url'}
        try:
            import httpx
            with httpx.Client(timeout=timeout_ms / 1000.0) as client:
                if method == 'GET':
                    resp = client.get(url, params=payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
                elif method == 'PUT':
                    resp = client.put(url, json=payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
                elif method == 'DELETE':
                    resp = client.delete(url, json=payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
                else:
                    resp = client.post(url, json=payload or {}, headers={k: str(v) for k, v in (headers.items() if isinstance(headers, dict) else [])})
            if resp.status_code >= 400:
                out = {'ok': False, 'error': f'http_{resp.status_code}: ' + (resp.text or '')[:200]}
                try:
                    lg = Log(user_id=current_user.id, level='error', action='skill.test', resource_type='skill', resource_id=row.id,
                        details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'webhook', 'http_status': resp.status_code}, ip_address=None)
                    db.add(lg); db.commit()
                except Exception:
                    db.rollback()
                return out
            try:
                data = resp.json()
            except Exception:
                data = {'text': resp.text}
            out = {'ok': True, 'result': data}
            try:
                lg = Log(user_id=current_user.id, level='info', action='skill.test', resource_type='skill', resource_id=row.id,
                    details={'ok': True, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'webhook'}, ip_address=None)
                db.add(lg); db.commit()
            except Exception:
                db.rollback()
            return out
        except Exception as e:
            out = {'ok': False, 'error': str(e)}
            try:
                lg = Log(user_id=current_user.id, level='error', action='skill.test', resource_type='skill', resource_id=row.id,
                    details={'ok': False, 'duration_ms': int((_time.time()-t0)*1000), 'type': 'webhook', 'error': str(e)[:200]}, ip_address=None)
                db.add(lg); db.commit()
            except Exception:
                db.rollback()
            return out


@router.get('/skills/{skill_id}/tests')
@require_permission('skills.read')
async def list_skill_tests(
    skill_id: str,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.query(Log).filter(Log.resource_type=='skill', Log.resource_id==skill_id, Log.action=='skill.test').order_by(Log.timestamp.desc()).limit(max(1,min(limit,100))).all()
    def _to(e: Log):
        try:
            return {'id': str(e.id), 'level': e.level, 'details': e.details or {}, 'timestamp': e.timestamp.isoformat() if e.timestamp else None}
        except Exception:
            return {'id': None, 'level': None, 'details': {}, 'timestamp': None}
    return {'items': [_to(x) for x in rows]}


@router.delete('/skills/{skill_id}/tests')
@require_permission('skills.update')
async def clear_skill_tests(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """清除指定技能的所有測試紀錄。"""
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)
    deleted = db.query(Log).filter(
        Log.resource_type == 'skill',
        Log.resource_id == skill_id,
        Log.action == 'skill.test'
    ).delete(synchronize_session=False)
    db.commit()
    return {'ok': True, 'deleted_count': deleted}


@router.post('/skills/{skill_id}/upload-zip')
@require_permission('skills.update')
async def upload_skill_zip(
    skill_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """為現有技能上傳 ZIP 檔案。

    會更新 zip_bundle、prompt_template、references 欄位。
    """
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)

    if not file.filename or not file.filename.lower().endswith('.zip'):
        raise validation_error('請上傳 .zip 檔案')

    content = await file.read()
    if not content:
        raise validation_error('檔案內容為空')

    parsed = _parse_claude_skill_zip(content)

    # 更新 ZIP 相關欄位
    row.zip_bundle = content

    # 若 ZIP 包含 SKILL.md，更新 prompt_template
    if parsed.get('skill_md') and parsed['skill_md'].get('prompt_template'):
        row.prompt_template = str(parsed['skill_md'].get('raw_content') or parsed['skill_md']['prompt_template'])

    # 更新 references
    if parsed.get('references'):
        row.references = parsed['references']

    # 若有腳本，更新為 hybrid
    if parsed.get('scripts') and row.skill_type == 'prompt':
        row.skill_type = 'hybrid'

    db.commit()
    db.refresh(row)

    return {'ok': True, 'skill': _to_dict(row), 'parsed': parsed}


@router.delete('/skills/{skill_id}/zip')
@require_permission('skills.update')
async def clear_skill_zip(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """清除技能的 ZIP 檔案。"""
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)

    row.zip_bundle = None
    db.commit()
    db.refresh(row)

    return {'ok': True, 'skill': _to_dict(row)}


@router.delete('/skills/{skill_id}')
@require_permission('skills.delete')
async def delete_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)

    # 目的：刪除技能前先解除互動紀錄的外鍵依賴。
    # 為什麼：skill_interactions 會保留歷史流程，若直接刪技能會觸發 FK violation。
    db.query(SkillInteraction).filter(SkillInteraction.skill_id == row.id).update(
        {SkillInteraction.skill_id: None},
        synchronize_session=False,
    )
    db.delete(row)
    db.commit()
    return {'ok': True}
