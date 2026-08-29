from fastapi import APIRouter, Depends, Body, UploadFile, File
from typing import Any
from sqlalchemy.orm import Session
import re
import yaml
import zipfile
import io
from pathlib import Path

from src.core.database import get_db
from src.models import FunctionProfile, User
from src.middleware.auth import get_current_user
from src.middleware.rbac import require_permission, check_permission
from src.services.tool_policy_service import ToolPolicyService
from src.api.errors import not_found_error, validation_error, forbidden_error


router = APIRouter()
tool_policy_service = ToolPolicyService()


def _parse_claude_skill(content: str) -> dict[str, str]:
    """解析 Claude Skill 內容（YAML 或 Markdown 格式），轉換為 FunctionProfile 格式。

    支援格式：
    1. YAML front matter：
       ---
       name: skill-name
       description: ...
       prompt: |
         ...
       ---

    2. 純 Markdown：
       # Skill Name
       Prompt content...
    """
    content = (content or '').strip()
    if not content:
        return {'name': '', 'description': '', 'template': ''}

    # 嘗試解析 YAML front matter
    yaml_match = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)', content, re.DOTALL)
    if yaml_match:
        try:
            meta = yaml.safe_load(yaml_match.group(1)) or {}
            body = yaml_match.group(2).strip()
            return {
                'name': str(meta.get('name') or '').strip(),
                'description': str(meta.get('description') or '').strip(),
                'template': str(meta.get('prompt') or body).strip()
            }
        except yaml.YAMLError:
            pass

    # 純 Markdown 格式
    lines = content.split('\n')
    name = ''
    start_idx = 0

    # 檢查是否有標題行
    if lines and lines[0].startswith('# '):
        name = lines[0][2:].strip()
        start_idx = 1

    template = '\n'.join(lines[start_idx:]).strip()
    return {'name': name, 'description': '', 'template': template}


def _to_dict(row: FunctionProfile) -> dict[str, Any]:
    template = row.template or ''
    return {
        'id': str(row.id),
        'name': row.name,
        'provider': row.provider or '',
        'function_definition_template': template,
        'template': template,
        'description': row.description or '',
        'enabled': bool(row.enabled),
        'version': int(row.version or 1),
        'references': row.references,  # Claude Skill references/ 內容
        # OpenAI Function Calling 格式欄位
        'parameters': row.parameters,  # JSON Schema
        'handler_type': row.handler_type or 'internal',
        'handler_config': row.handler_config,
        'execution_policy': tool_policy_service.normalize_policy(row.execution_policy if isinstance(row.execution_policy, dict) else {}),
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post('/functions/import-claude-skill')
@require_permission('functions.create')
async def import_claude_skill(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """解析 Claude Skill 內容，回傳解析結果供預覽或直接建立。

    Request:
        content: Claude Skill 內容（YAML 或 Markdown 格式）
        create: 是否直接建立（預設 False，僅解析預覽）

    Response:
        ok: True
        parsed: { name, description, template }
        created: (若 create=True) 建立的 FunctionProfile
    """
    content = str((payload or {}).get('content') or '').strip()
    if not content:
        raise validation_error('content 為必填')

    parsed = _parse_claude_skill(content)

    if not parsed.get('template'):
        raise validation_error('無法解析出有效的 template 內容')

    result: dict[str, Any] = {'ok': True, 'parsed': parsed}

    # 若指定 create=True，直接建立 FunctionProfile
    if (payload or {}).get('create'):
        name = parsed.get('name') or 'imported-skill'
        # 檢查名稱是否已存在
        exists = db.query(FunctionProfile).filter(FunctionProfile.name == name).first()
        if exists:
            # 自動加上後綴
            base_name = name
            for i in range(2, 100):
                name = f"{base_name}-{i}"
                if not db.query(FunctionProfile).filter(FunctionProfile.name == name).first():
                    break

        row = FunctionProfile(
            name=name,
            description=parsed.get('description') or '',
            template=parsed.get('template'),
            enabled=True,
            version=1,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        result['created'] = _to_dict(row)

    return result


def _parse_claude_skill_zip(content: bytes) -> dict[str, Any]:
    """解析 Claude Skill ZIP 檔案。

    ZIP 結構範例：
        skill-name/
        ├── SKILL.md           # 必需：提示詞模板
        ├── scripts/           # 選用：可執行腳本
        │   └── process.py
        └── references/        # 選用：參考文檔
            └── guide.md

    回傳：
        skill_md: { name, description, template }
        scripts: [{ filename, type, code }]
        references: [{ filename, content }]
    """
    result: dict[str, Any] = {
        'skill_md': None,
        'scripts': [],
        'references': []
    }

    try:
        with zipfile.ZipFile(io.BytesIO(content), 'r') as zf:
            for name in zf.namelist():
                # 跳過目錄
                if name.endswith('/'):
                    continue

                path = Path(name)
                parts = path.parts

                # 找 SKILL.md（可能在根目錄或子目錄）
                if path.name.upper() == 'SKILL.MD':
                    text = zf.read(name).decode('utf-8')
                    result['skill_md'] = _parse_claude_skill(text)

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
                        pass  # 跳過無法解碼的二進位檔

                # 找 references/ 下的 Markdown 文件
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


@router.post('/functions/import-claude-skill-zip')
@require_permission('functions.create')
async def import_claude_skill_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """上傳並解析 Claude Skill ZIP 檔案。

    Request: multipart/form-data
        file: ZIP 檔案

    Query params:
        create: 是否直接建立（預設 False，僅解析預覽）
        include_references: 是否將 references 存入 DB（預設 True）

    Response:
        ok: True
        parsed: { skill_md, scripts, references }
        created: (若 create=True) 建立的 FunctionProfile
    """
    if not file.filename or not file.filename.lower().endswith('.zip'):
        raise validation_error('請上傳 .zip 檔案')

    content = await file.read()
    if not content:
        raise validation_error('檔案內容為空')

    parsed = _parse_claude_skill_zip(content)

    if not parsed.get('skill_md') or not parsed['skill_md'].get('template'):
        raise validation_error('ZIP 中未找到有效的 SKILL.md')

    result: dict[str, Any] = {'ok': True, 'parsed': parsed}

    return result


@router.post('/functions/import-claude-skill-zip/create')
@require_permission('functions.create')
async def create_from_claude_skill_zip(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """根據已解析的 ZIP 內容建立 FunctionProfile。

    Request:
        skill_md: { name, description, template }
        references: [{ filename, content }]（可選）
        include_references: 是否存入 references 欄位（預設 True）

    Response:
        ok: True
        created: 建立的 FunctionProfile
    """
    skill_md = (payload or {}).get('skill_md')
    if not skill_md or not skill_md.get('template'):
        raise validation_error('skill_md.template 為必填')

    name = str(skill_md.get('name') or 'imported-skill').strip()

    # 檢查名稱是否已存在
    exists = db.query(FunctionProfile).filter(FunctionProfile.name == name).first()
    if exists:
        base_name = name
        for i in range(2, 100):
            name = f"{base_name}-{i}"
            if not db.query(FunctionProfile).filter(FunctionProfile.name == name).first():
                break

    # 處理 references
    references_data = None
    if (payload or {}).get('include_references', True):
        refs = (payload or {}).get('references') or []
        if refs:
            references_data = refs

    row = FunctionProfile(
        name=name,
        description=str(skill_md.get('description') or ''),
        template=str(skill_md.get('template') or ''),
        enabled=True,
        version=1,
        references=references_data,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    return {'ok': True, 'created': _to_dict(row)}


@router.get('/functions')
@require_permission('functions.read')
async def list_functions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.query(FunctionProfile).order_by(FunctionProfile.created_at.desc()).all()
    return {'functions': [_to_dict(r) for r in rows]}


@router.get('/functions/selectable')
async def list_selectable_functions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent', db=db):
        raise forbidden_error()
    rows = db.query(FunctionProfile).filter(FunctionProfile.enabled == True).order_by(FunctionProfile.created_at.desc()).all()  # noqa: E712
    return {'functions': [_to_dict(r) for r in rows]}


@router.post('/functions')
@require_permission('functions.create')
async def create_function(
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """建立 Function Profile。

    支援兩種格式：
    1. 舊版（提示詞模板）：需提供 template 或 function_definition_template
    2. OpenAI Function Calling：需提供 parameters（JSON Schema）

    至少需要提供其中一種。
    """
    name = str((payload or {}).get('name') or '').strip()
    if not name:
        raise validation_error('name 為必填')

    # 處理舊版 template
    template_value = (payload or {}).get('function_definition_template')
    if template_value is None:
        template_value = (payload or {}).get('template')
    template = str(template_value or '').strip() if template_value else None

    # 處理 OpenAI 格式
    parameters = (payload or {}).get('parameters')
    handler_type = str((payload or {}).get('handler_type') or 'internal').strip()
    handler_config = (payload or {}).get('handler_config')

    # 至少需要 template 或 parameters
    if not template and not parameters:
        raise validation_error('需提供 template 或 parameters')

    exists = db.query(FunctionProfile).filter(FunctionProfile.name == name).first()
    if exists:
        raise validation_error('名稱已存在')

    row = FunctionProfile(
        name=name,
        provider=str((payload or {}).get('provider') or '').strip() or None,
        template=template,
        description=str((payload or {}).get('description') or ''),
        enabled=bool((payload or {}).get('enabled', True)),
        version=int((payload or {}).get('version') or 1),
        references=(payload or {}).get('references'),
        # OpenAI Function Calling 欄位
        parameters=parameters,
        handler_type=handler_type if handler_type in ('internal', 'webhook', 'mcp') else 'internal',
        handler_config=handler_config,
        execution_policy=tool_policy_service.normalize_policy((payload or {}).get('execution_policy') if isinstance((payload or {}).get('execution_policy'), dict) else {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.get('/functions/{function_id}')
@require_permission('functions.read')
async def get_function(
    function_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(FunctionProfile).filter(FunctionProfile.id == function_id).first()
    if not row:
        raise not_found_error('Function', function_id)
    return _to_dict(row)


@router.put('/functions/{function_id}')
@require_permission('functions.update')
async def update_function(
    function_id: str,
    payload: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新 Function Profile。

    支援兩種格式：
    1. 舊版（提示詞模板）：template 或 function_definition_template
    2. OpenAI Function Calling：parameters（JSON Schema）+ handler_type + handler_config

    至少需保留其中一種。
    """
    row = db.query(FunctionProfile).filter(FunctionProfile.id == function_id).first()
    if not row:
        raise not_found_error('Function', function_id)

    if 'name' in (payload or {}):
        name = str((payload or {}).get('name') or '').strip()
        if not name:
            raise validation_error('name 不可為空')
        dup = db.query(FunctionProfile).filter(FunctionProfile.name == name, FunctionProfile.id != row.id).first()
        if dup:
            raise validation_error('名稱已存在')
        row.name = name

    # 基本欄位
    for key in ('provider', 'description', 'enabled', 'version', 'references'):
        if key in (payload or {}):
            setattr(row, key, (payload or {}).get(key))

    # 舊版 template 欄位
    if 'function_definition_template' in (payload or {}) or 'template' in (payload or {}):
        template_value = (payload or {}).get('function_definition_template')
        if template_value is None:
            template_value = (payload or {}).get('template')
        row.template = str(template_value or '') if template_value else None

    # OpenAI Function Calling 欄位
    if 'parameters' in (payload or {}):
        row.parameters = (payload or {}).get('parameters')

    if 'handler_type' in (payload or {}):
        ht = str((payload or {}).get('handler_type') or 'internal').strip()
        row.handler_type = ht if ht in ('internal', 'webhook', 'mcp') else 'internal'

    if 'handler_config' in (payload or {}):
        row.handler_config = (payload or {}).get('handler_config')

    if 'execution_policy' in (payload or {}):
        row.execution_policy = tool_policy_service.normalize_policy(
            (payload or {}).get('execution_policy') if isinstance((payload or {}).get('execution_policy'), dict) else {}
        )

    # 驗證：至少需有 template 或 parameters
    if not str(row.template or '').strip() and not row.parameters:
        raise validation_error('需保留 template 或 parameters')

    db.commit()
    db.refresh(row)
    return _to_dict(row)


@router.delete('/functions/{function_id}')
@require_permission('functions.delete')
async def delete_function(
    function_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(FunctionProfile).filter(FunctionProfile.id == function_id).first()
    if not row:
        raise not_found_error('Function', function_id)
    db.delete(row)
    db.commit()
    return {'ok': True, 'id': function_id}
