from fastapi import APIRouter, Depends, Body, UploadFile, File
from typing import Any
from sqlalchemy.orm import Session
import zipfile
import io
from pathlib import Path
import re
import yaml

from src.core.database import get_db
from src.models import SkillEntry, User, Log
from src.middleware.auth import get_current_user
from src.middleware.rbac import require_role, Role
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, validation_error


router = APIRouter()


def _to_dict(s: SkillEntry) -> dict[str, Any]:
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
        'input_schema': s.input_schema or {},
        # Claude Skills 相容欄位
        'skill_type': s.skill_type or 'executable',
        'prompt_template': s.prompt_template or '',
        'has_zip': bool(s.zip_bundle),  # 不回傳整個 ZIP，僅回傳是否存在
        'references': s.references,
        'created_at': s.created_at.isoformat() if s.created_at else None,
        'updated_at': s.updated_at.isoformat() if s.updated_at else None,
    }


@router.get('/skills')
@require_role([Role.ADMIN])
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
    if not check_permission(current_user, 'update_agent'):
        from src.api.errors import forbidden_error
        raise forbidden_error()
    rows = db.query(SkillEntry).filter(SkillEntry.enabled == True).order_by(SkillEntry.created_at.desc()).all()  # noqa: E712
    return {'skills': [_to_dict(r) for r in rows]}


# ========== Claude Skills ZIP 匯入 ==========

def _parse_skill_md(content: str) -> dict[str, str]:
    """解析 SKILL.md 內容（YAML front matter 或純 Markdown）。"""
    content = (content or '').strip()
    if not content:
        return {'name': '', 'description': '', 'prompt_template': ''}

    # 嘗試解析 YAML front matter
    yaml_match = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)', content, re.DOTALL)
    if yaml_match:
        try:
            meta = yaml.safe_load(yaml_match.group(1)) or {}
            body = yaml_match.group(2).strip()
            return {
                'name': str(meta.get('name') or '').strip(),
                'description': str(meta.get('description') or '').strip(),
                'prompt_template': str(meta.get('prompt') or body).strip()
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
    return {'name': name, 'description': '', 'prompt_template': prompt_template}


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
                        script_type = 'python' if ext == '.py' else 'bash' if ext == '.sh' else 'other'
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


@router.post('/skills/import-claude-skill-zip')
@require_role([Role.ADMIN])
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
@require_role([Role.ADMIN])
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
        prompt_template=str(skill_md.get('prompt_template') or ''),
        zip_bundle=content,  # 儲存完整 ZIP
        references=parsed.get('references') or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {'ok': True, 'skill': _to_dict(row)}


@router.post('/skills')
@require_role([Role.ADMIN])
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
    typ = (payload or {}).get('type') or 'webhook'
    if typ not in ('webhook', 'python'):
        raise validation_error('type 必須為 webhook 或 python')
    endpoint_url = (payload or {}).get('endpoint_url') or None
    http_method = ((payload or {}).get('http_method') or 'POST').upper()
    headers = (payload or {}).get('headers') or {}
    timeout_ms = (payload or {}).get('timeout_ms') or 8000
    python_handler = (payload or {}).get('python_handler') or None
    input_schema = (payload or {}).get('input_schema') or {}

    if typ == 'webhook' and (not endpoint_url or not isinstance(endpoint_url, str)):
        raise validation_error('webhook 類型需要 endpoint_url')
    if typ == 'python' and (not python_handler or not isinstance(python_handler, str)):
        raise validation_error('python 類型需要 python_handler')
    if not isinstance(headers, dict):
        raise validation_error('headers 必須為 JSON 物件')
    try:
        timeout_ms = int(timeout_ms)
    except Exception:
        timeout_ms = 8000
    if timeout_ms <= 0:
        timeout_ms = 8000

    # Claude Skills 相容欄位
    skill_type = str((payload or {}).get('skill_type') or 'executable').strip()
    if skill_type not in ('prompt', 'executable', 'hybrid'):
        skill_type = 'executable'
    prompt_template = (payload or {}).get('prompt_template') or None
    references = (payload or {}).get('references') or None

    s = SkillEntry(
        name=name.strip(),
        description=(payload or {}).get('description') or '',
        enabled=bool((payload or {}).get('enabled', True)),
        type=typ,
        endpoint_url=endpoint_url,
        http_method=http_method,
        headers=headers,
        timeout_ms=timeout_ms,
        python_handler=python_handler,
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
@require_role([Role.ADMIN])
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
@require_role([Role.ADMIN])
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
    for k in ('description','enabled','type','endpoint_url','http_method','headers','timeout_ms','python_handler','input_schema',
               'skill_type','prompt_template','references'):
        if k in (payload or {}):
            val = (payload or {}).get(k)
            # skill_type 驗證
            if k == 'skill_type' and val not in ('prompt', 'executable', 'hybrid', None):
                val = 'executable'
            setattr(row, k, val)
    # 基本驗證
    if row.type not in ('webhook', 'python'):
        raise validation_error('type 必須為 webhook 或 python')
    if row.type == 'webhook':
        if not row.endpoint_url:
            raise validation_error('webhook 類型需要 endpoint_url')
        row.http_method = (row.http_method or 'POST').upper()
        if not isinstance(row.headers, dict):
            row.headers = {}
        try:
            row.timeout_ms = int(row.timeout_ms or 8000)
        except Exception:
            row.timeout_ms = 8000
        if row.timeout_ms <= 0:
            row.timeout_ms = 8000
    if row.type == 'python':
        if not row.python_handler:
            raise validation_error('python 類型需要 python_handler')
    if row.input_schema is not None and not isinstance(row.input_schema, dict):
        row.input_schema = {}
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.post('/skills/{skill_id}/test')
@require_role([Role.ADMIN])
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
        try:
            timeout_ms = int(getattr(row, 'timeout_ms', 8000) or 8000)
        except Exception:
            timeout_ms = 8000
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
@require_role([Role.ADMIN])
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
@require_role([Role.ADMIN])
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
@require_role([Role.ADMIN])
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
        row.prompt_template = parsed['skill_md']['prompt_template']

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
@require_role([Role.ADMIN])
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
@require_role([Role.ADMIN])
async def delete_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(SkillEntry).filter(SkillEntry.id == skill_id).first()
    if not row:
        raise not_found_error('Skill', skill_id)
    db.delete(row)
    db.commit()
    return {'ok': True}
