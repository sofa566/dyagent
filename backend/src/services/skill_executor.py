"""Skill 執行服務。

負責執行 Claude Skills 格式的技能：
1. 解壓 ZIP 到暫存目錄
2. 執行 scripts/ 下的腳本
3. 合併 prompt_template 與腳本輸出

此服務使用 DATA_CACHE_PATH 作為暫存目錄。
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.config import settings
from src.core.logging import get_logger

_log = get_logger('skill_executor')
@dataclass
class SkillExecutionResult:
    """技能執行結果。"""
    ok: bool
    prompt: str = ''
    script_outputs: list[dict[str, Any]] | None = None
    error: str | None = None
    work_dir: str | None = None


def _ensure_cache_dir() -> Path:
    """確保快取目錄存在。"""
    cache_path = Path(settings.DATA_CACHE_PATH)
    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path


def extract_skill_zip(zip_content: bytes, skill_id: str) -> Path:
    """將 ZIP 解壓到暫存目錄。

    Args:
        zip_content: ZIP 檔案內容
        skill_id: 技能 ID（用於建立唯一目錄）

    Returns:
        解壓後的目錄路徑
    """
    cache_dir = _ensure_cache_dir()
    work_dir = cache_dir / f"skill-{skill_id}-{uuid.uuid4().hex[:8]}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_content), 'r') as zf:
            for member in zf.infolist():
                member_name = str(member.filename or '')
                if not member_name or member_name.endswith('/'):
                    continue
                normalized_path = Path(member_name)
                if normalized_path.is_absolute() or '..' in normalized_path.parts:
                    raise ValueError(f"ZIP 包含不安全路徑: {member_name}")
                target_path = (work_dir / normalized_path).resolve()
                if not str(target_path).startswith(str(work_dir.resolve())):
                    raise ValueError(f"ZIP 路徑超出工作目錄: {member_name}")
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, 'r') as src, open(target_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
    except zipfile.BadZipFile as error:
        raise ValueError(f"無效的 ZIP 檔案: {error}") from error

    return work_dir


def cleanup_work_dir(work_dir: Path) -> None:
    """清理暫存工作目錄。"""
    if work_dir and work_dir.exists():
        try:
            shutil.rmtree(work_dir)
        except Exception:
            pass


def extract_skill_ui_dir(zip_content: bytes, skill_id: str) -> Path:
    # 目的：解壓技能 ZIP 的 UI 內容到可重用快取目錄。
    # 為什麼：避免每次請求都重複解壓，降低 I/O 成本並提供穩定靜態資源路徑。
    cache_dir = _ensure_cache_dir() / 'skill-ui'
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_hash = hashlib.sha256(zip_content).hexdigest()
    skill_cache_dir = cache_dir / f'{skill_id}-{zip_hash[:16]}'
    if skill_cache_dir.exists() and (skill_cache_dir / '.ready').exists():
        return skill_cache_dir

    if skill_cache_dir.exists():
        shutil.rmtree(skill_cache_dir, ignore_errors=True)
    skill_cache_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(zip_content), 'r') as zf:
        for member in zf.infolist():
            member_name = str(member.filename or '')
            if not member_name or member_name.endswith('/'):
                continue
            normalized_path = Path(member_name)
            if normalized_path.is_absolute() or '..' in normalized_path.parts:
                raise ValueError(f'ZIP 包含不安全路徑: {member_name}')
            target_path = (skill_cache_dir / normalized_path).resolve()
            if not str(target_path).startswith(str(skill_cache_dir.resolve())):
                raise ValueError(f'ZIP 路徑超出工作目錄: {member_name}')
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member, 'r') as src, open(target_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)

    ready_flag_path = skill_cache_dir / '.ready'
    ready_flag_path.write_text('ok', encoding='utf-8')
    return skill_cache_dir


def resolve_skill_ui_asset(base_dir: Path, asset_path: str) -> Path:
    # 目的：解析並驗證技能 UI 資源路徑。
    # 為什麼：限制只能讀取 ui/ 目錄，防止路徑穿越或讀取非預期檔案。
    normalized_asset = str(asset_path or '').strip().lstrip('/')
    if not normalized_asset:
        raise ValueError('asset_path_required')
    relative_path = Path(normalized_asset)
    if relative_path.is_absolute() or '..' in relative_path.parts:
        raise ValueError('invalid_asset_path')
    if not relative_path.parts or relative_path.parts[0] != 'ui':
        raise ValueError('asset_outside_ui_dir')

    target_path = (base_dir / relative_path).resolve()
    if not str(target_path).startswith(str(base_dir.resolve())):
        raise ValueError('asset_outside_work_dir')
    if not target_path.exists() or not target_path.is_file():
        fallback_path = _resolve_nested_ui_asset(base_dir=base_dir, relative_path=relative_path)
        if fallback_path is None:
            raise FileNotFoundError('asset_not_found')
        target_path = fallback_path
    return target_path


def _resolve_nested_ui_asset(base_dir: Path, relative_path: Path) -> Path | None:
    # 目的：支援 ZIP 外層包一層資料夾時的 UI 路徑解析。
    # 為什麼：實務上很多壓縮檔會是 `skill-name/ui/index.html`，不能只接受根目錄直接有 `ui/`。
    parts = relative_path.parts
    if not parts or parts[0] != 'ui':
        return None

    nested_relative = Path(*parts[1:]) if len(parts) > 1 else Path('index.html')
    candidate_paths = list(base_dir.glob('*/ui/*'))
    if not candidate_paths:
        return None

    for candidate in base_dir.glob('*/ui'):
        resolved_candidate = (candidate / nested_relative).resolve()
        if not str(resolved_candidate).startswith(str(base_dir.resolve())):
            continue
        if resolved_candidate.exists() and resolved_candidate.is_file():
            return resolved_candidate
    return None


def _find_scripts_dir(work_dir: Path) -> Path | None:
    """找到 scripts/ 目錄（可能在根目錄或子目錄）。"""
    # 直接在根目錄
    if (work_dir / 'scripts').is_dir():
        return work_dir / 'scripts'

    # 在子目錄中（ZIP 可能包含頂層目錄）
    for child in work_dir.iterdir():
        if child.is_dir():
            scripts_dir = child / 'scripts'
            if scripts_dir.is_dir():
                return scripts_dir

    return None


def _find_skill_md(work_dir: Path) -> Path | None:
    """找到 SKILL.md 檔案。"""
    for candidate in [
        work_dir / 'SKILL.md',
        work_dir / 'SKILL.MD',
        work_dir / 'skill.md',
    ]:
        if candidate.exists():
            return candidate

    # 在子目錄中搜尋
    for child in work_dir.iterdir():
        if child.is_dir():
            for name in ['SKILL.md', 'SKILL.MD', 'skill.md']:
                candidate = child / name
                if candidate.exists():
                    return candidate

    return None


def execute_script(script_path: Path, work_dir: Path, input_data: dict[str, Any] | None = None,
                   timeout_seconds: int = 30, extra_args: list[str] | None = None) -> dict[str, Any]:
    """執行單一腳本。

    Args:
        script_path: 腳本路徑
        work_dir: 工作目錄
        input_data: 傳入腳本的 JSON 資料（透過環境變數 SKILL_INPUT）
        timeout_seconds: 逾時秒數

    Returns:
        包含 stdout, stderr, return_code 的 dict
    """
    ext = script_path.suffix.lower()

    # 決定執行命令
    if ext == '.py':
        cmd = [sys.executable, str(script_path)]
    elif ext in {'.js', '.mjs'}:
        node_bin = shutil.which('node')
        if not node_bin:
            return {
                'script': script_path.name,
                'ok': False,
                'return_code': -1,
                'stdout': '',
                'stderr': 'runner_not_found: node',
            }
        cmd = [node_bin, str(script_path)]
    elif ext == '.sh':
        bash_bin = shutil.which('bash')
        if not bash_bin:
            return {
                'script': script_path.name,
                'ok': False,
                'return_code': -1,
                'stdout': '',
                'stderr': 'runner_not_found: bash',
            }
        cmd = [bash_bin, str(script_path)]
    else:
        return {
            'script': script_path.name,
            'ok': False,
            'return_code': -1,
            'stdout': '',
            'stderr': f'unsupported_script_extension: {ext}',
        }

    if isinstance(extra_args, list) and extra_args:
        cmd.extend([str(x) for x in extra_args])

    # 準備環境變數
    env = os.environ.copy()
    if input_data:
        import json
        env['SKILL_INPUT'] = json.dumps(input_data, ensure_ascii=False)
    runtime_python_path = env.get('PYTHONPATH', '')
    runtime_python_root = str(work_dir)
    if runtime_python_path:
        env['PYTHONPATH'] = f"{runtime_python_root}:{runtime_python_path}"
    else:
        env['PYTHONPATH'] = runtime_python_root

    try:
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return {
            'script': script_path.name,
            'ok': result.returncode == 0,
            'return_code': result.returncode,
            'stdout': result.stdout,
            'stderr': result.stderr,
        }
    except subprocess.TimeoutExpired:
        return {
            'script': script_path.name,
            'ok': False,
            'return_code': -1,
            'stdout': '',
            'stderr': f'執行逾時 ({timeout_seconds}s)',
        }
    except Exception as e:
        return {
            'script': script_path.name,
            'ok': False,
            'return_code': -1,
            'stdout': '',
            'stderr': str(e),
        }


def _extract_script_controls(input_data: dict[str, Any] | None) -> tuple[str | None, list[str], dict[str, Any]]:
    """目的：從輸入資料擷取腳本執行控制參數。
    為什麼：避免盲目執行所有腳本，改為顯式指定或慣例入口，降低誤觸與參數缺失錯誤。
    """
    # 檢查輸入資料是否為字典類型，若不是則返回預設值
    if not isinstance(input_data, dict):
        return None, [], {}
    # 從輸入資料中獲取腳本名稱，並進行類型檢查與格式化
    script_name = input_data.get('_script')
    # 確保腳本名稱為非空字符串，否則設為None
    script_name_str = str(script_name).strip() if isinstance(script_name, str) and script_name.strip() else None
    # 從輸入資料中獲取腳本參數列表
    raw_args = input_data.get('_args')
    args: list[str] = []
    # 確保參數為列表類型，並將所有元素轉換為字符串
    if isinstance(raw_args, list):
        args = [str(x) for x in raw_args]
    # 創建一個新的字典，排除腳本名稱和參數，保留其他數據作為負載
    payload = {k: v for k, v in input_data.items() if k not in {'_script', '_args'}}
    return script_name_str, args, payload


def _resolve_entry_script(scripts_dir: Path, script_name: str | None) -> Path | None:
    """目的：解析本輪應執行的入口腳本。
    為什麼：Claude Skill 的 scripts 多為工具庫，不應全部執行；需入口腳本導向實作流程。
    """
    script_files = sorted(
        [f for f in scripts_dir.iterdir() if f.is_file() and f.suffix.lower() in ('.py', '.js', '.mjs', '.sh')],
        key=lambda x: x.name,
    )
    if not script_files:
        return None

    if script_name:
        for f in script_files:
            if f.name == script_name:
                return f
        return None

    preferred = (
        'main.py', 'main.js', 'main.mjs', 'main.sh',
        'run.py', 'run.js', 'run.mjs', 'run.sh',
        'index.py', 'index.js', 'index.mjs', 'index.sh',
    )
    for name in preferred:
        for f in script_files:
            if f.name == name:
                return f
    return None


def _extract_yaml_scalar(text: str, key: str) -> str:
    # 目的：從 YAML front matter 取出單行欄位值。
    # 為什麼：SKILL.md 的固定欄位常放在 front matter，需先做穩定萃取再交給模型補齊。
    if not text.strip() or not key.strip():
        return ''
    pattern = rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$"
    matched = re.search(pattern, text, flags=re.MULTILINE)
    if matched is None:
        return ''
    raw_value = str(matched.group(1) or '').strip()
    if (raw_value.startswith('"') and raw_value.endswith('"')) or (raw_value.startswith("'") and raw_value.endswith("'")):
        return raw_value[1:-1].strip()
    return raw_value


def _extract_skill_md_fixed_fields(skill_md_text: str) -> tuple[str, str]:
    # 目的：先以規則萃取 name/description 固定欄位。
    # 為什麼：name/description 是唯一穩定欄位，應優先確保一致，再讓 LLM 解譯其餘自由內容。
    content = str(skill_md_text or '').strip()
    if not content:
        return '', ''

    name = ''
    description = ''

    front_matter = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', content, flags=re.DOTALL)
    if front_matter is not None:
        metadata = str(front_matter.group(1) or '')
        body = str(front_matter.group(2) or '').strip()
        name = _extract_yaml_scalar(metadata, 'name')
        description = _extract_yaml_scalar(metadata, 'description')
        if not description and body:
            first_body_line = next((line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith('#')), '')
            description = first_body_line

    if not name:
        heading = re.search(r'^#\s+(.+)$', content, flags=re.MULTILINE)
        if heading is not None:
            name = str(heading.group(1) or '').strip()

    if not description:
        non_heading_lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith('#')]
        if non_heading_lines:
            description = non_heading_lines[0]

    return name, description


def _extract_first_json_object(raw_text: str) -> dict[str, Any] | None:
    # 目的：從 LLM 回覆中擷取第一個 JSON 物件。
    # 為什麼：模型偶爾會夾帶說明文字，需容錯萃取避免整體流程失敗。
    text = str(raw_text or '').strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass

    start_index = text.find('{')
    end_index = text.rfind('}')
    if start_index < 0 or end_index <= start_index:
        return None
    try:
        parsed = json.loads(text[start_index:end_index + 1])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _default_skill_md_llm_complete(prompt: str) -> str:
    # 目的：提供 skill_to_json 的預設 LLM 呼叫器。
    # 為什麼：避免 skill_executor 直接耦合上層路由，仍可在無注入 callback 時獨立工作。
    from src.services.llm_client import LLMClient

    llm = LLMClient()
    session_id = f"skill-md-parser-{uuid.uuid4().hex[:8]}"
    try:
        llm.init_for_session(session_id=session_id)
    except TypeError:
        llm.init_for_session(session_id=session_id, preferred_tier=None)
    return str(llm.complete(prompt=prompt, tier=None) or '').strip()


def skill_to_json(
    *,
    skill_md_text: str,
    llm_complete: Callable[[str], str] | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    # 目的：將 SKILL.md 轉換為結構化 JSON（name/description 固定，其餘由 LLM 解譯）。
    # 為什麼：SKILL.md 內容高度自由，單靠規則難以完整覆蓋，需要模型做語意解譯並輸出統一格式。
    source_text = str(skill_md_text or '').strip()
    if not source_text:
        return {'ok': False, 'error': 'empty_skill_md', 'skill_json': {}}

    fixed_name, fixed_description = _extract_skill_md_fixed_fields(source_text)
    llm_runner = llm_complete or _default_skill_md_llm_complete
    prompt = (
        '你是技能規格解析器。請將使用者提供的 SKILL.md 轉為單一 JSON 物件。\n'
        '嚴格規則：\n'
        '1) 僅輸出 JSON，不可輸出 markdown 或解釋文字。\n'
        '2) 必須包含 name、description 兩個欄位。\n'
        '3) 不可憑空捏造資訊；不確定請填 null 或空陣列。\n'
        '4) 其餘自由內容請盡可能放到 fields（物件）、steps（陣列）、examples（陣列）、constraints（陣列）。\n'
        '5) name 與 description 優先使用提供的固定值。\n\n'
        f'固定 name：{fixed_name or ""}\n'
        f'固定 description：{fixed_description or ""}\n\n'
        '[SKILL.md]\n'
        f'{source_text}\n'
    )

    try:
        raw_output = str(llm_runner(prompt) or '').strip()
    except Exception as error:
        fallback_json = {
            'name': fixed_name,
            'description': fixed_description,
            'fields': {},
            'steps': [],
            'examples': [],
            'constraints': [],
            'source_markdown': source_text,
        }
        return {'ok': False, 'error': f'llm_error: {error}', 'skill_json': fallback_json}

    parsed_json = _extract_first_json_object(raw_output)
    if parsed_json is None:
        fallback_json = {
            'name': fixed_name,
            'description': fixed_description,
            'fields': {},
            'steps': [],
            'examples': [],
            'constraints': [],
            'source_markdown': source_text,
        }
        return {
            'ok': False,
            'error': 'invalid_llm_json_output',
            'skill_json': fallback_json,
            'raw_output': raw_output,
        }

    normalized_json = dict(parsed_json)
    normalized_json['name'] = str(fixed_name or normalized_json.get('name') or '').strip()
    normalized_json['description'] = str(fixed_description or normalized_json.get('description') or '').strip()
    if not isinstance(normalized_json.get('fields'), dict):
        normalized_json['fields'] = {}
    for array_field in ('steps', 'examples', 'constraints'):
        if not isinstance(normalized_json.get(array_field), list):
            normalized_json[array_field] = []
    normalized_json['source_markdown'] = source_text

    output_file_path: str | None = None
    if output_path is not None:
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(normalized_json, ensure_ascii=False, indent=2), encoding='utf-8')
        output_file_path = str(output_file)

    return {
        'ok': True,
        'skill_json': normalized_json,
        'raw_output': raw_output,
        'output_path': output_file_path,
    }

class SkillExecutor:
    # 目的：封裝 Skill 執行流程（解壓、腳本執行、prompt 組裝）於同一個類別。
    # 為什麼：集中流程控制可降低閱讀與維護成本，並避免分散式狀態造成分支錯誤。

    def execute_skill(
        self,
        *,
        skill_id: str,
        zip_bundle: bytes | None,
        prompt_template: str | None,
        input_data: dict[str, Any] | None = None,
        execute_scripts: bool = True,
        timeout_seconds: int = 30,
    ) -> SkillExecutionResult:
        work_dir: Path | None = None
        script_outputs: list[dict[str, Any]] = []

        try:
            _log.debug(
                "Starting skill execution",
                skill_id=skill_id,
                has_zip_bundle=bool(zip_bundle),
                prompt_template_present=bool(prompt_template),
                input_data_keys=list(input_data.keys()) if input_data else None,
                execute_scripts=execute_scripts,
                timeout_seconds=timeout_seconds,
            )

            script_name, script_args, pure_input_data = _extract_script_controls(input_data)
            _log.debug(
                "Extracted script controls",
                script_name=script_name,
                script_args=script_args,
                pure_input_data_keys=list(pure_input_data.keys()) if pure_input_data else None,
            )

            if zip_bundle:
                work_dir = extract_skill_zip(zip_bundle, skill_id)
                script_outputs = self._collect_script_outputs(
                    work_dir=work_dir,
                    execute_scripts=execute_scripts,
                    script_name=script_name,
                    script_args=script_args,
                    input_data=pure_input_data,
                    timeout_seconds=timeout_seconds,
                )

            final_prompt = self._build_final_prompt(
                prompt_template=prompt_template,
                script_outputs=script_outputs,
            )

            return SkillExecutionResult(
                ok=True,
                prompt=final_prompt,
                script_outputs=script_outputs if script_outputs else None,
                work_dir=str(work_dir) if work_dir else None,
            )
        except Exception as error:
            return SkillExecutionResult(
                ok=False,
                error=str(error),
                work_dir=str(work_dir) if work_dir else None,
            )

    def _collect_script_outputs(
        self,
        *,
        work_dir: Path,
        execute_scripts: bool,
        script_name: str | None,
        script_args: list[str],
        input_data: dict[str, Any],
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        # 目的：集中處理是否執行 scripts 的分支邏輯並回傳標準輸出。
        # 為什麼：避免 execute_scripts=False 時落入不完整流程，造成空 prompt 或不一致行為。
        if not execute_scripts:
            if script_name:
                _log.info("Scripts execution disabled; explicit script ignored", script_name=script_name)
            return []

        scripts_dir = _find_scripts_dir(work_dir)
        if not scripts_dir:
            _log.debug("No scripts directory found in skill bundle", work_dir=str(work_dir))
            return []

        entry_script = _resolve_entry_script(scripts_dir, script_name)
        if script_name and entry_script is None:
            raise ValueError(f'script_not_found: {script_name}')
        if entry_script is None:
            _log.debug("No executable entry script resolved", scripts_dir=str(scripts_dir))
            return []

        runtime_work_dir = entry_script.parent.parent if entry_script.parent.name == 'scripts' else work_dir
        output = execute_script(
            entry_script,
            runtime_work_dir,
            input_data,
            timeout_seconds,
            extra_args=script_args,
        )
        return [output]

    def _build_final_prompt(
        self,
        *,
        prompt_template: str | None,
        script_outputs: list[dict[str, Any]],
    ) -> str:
        # 目的：產生最終可交給 LLM 的 prompt。
        # 為什麼：執行期一律以 DB 的 prompt_template 為準，避免 ZIP 內舊版 SKILL.md 造成覆蓋。
        final_prompt = str(prompt_template or '').strip()

        if script_outputs:
            outputs_text = '\n\n--- Script Outputs ---\n'
            for output in script_outputs:
                outputs_text += f"\n[{output['script']}]\n"
                if output.get('stdout'):
                    outputs_text += str(output['stdout'])
                if output.get('stderr') and not output.get('ok'):
                    outputs_text += f"\n[Error] {output['stderr']}"
            final_prompt = f"{final_prompt}{outputs_text}" if final_prompt else outputs_text

        return final_prompt


_default_skill_executor = SkillExecutor()


def execute_skill(
    skill_id: str,
    zip_bundle: bytes | None,
    prompt_template: str | None,
    input_data: dict[str, Any] | None = None,
    execute_scripts: bool = True,
    timeout_seconds: int = 30,
) -> SkillExecutionResult:
    """執行 Claude Skill（相容既有函式介面）。"""
    return _default_skill_executor.execute_skill(
        skill_id=skill_id,
        zip_bundle=zip_bundle,
        prompt_template=prompt_template,
        input_data=input_data,
        execute_scripts=execute_scripts,
        timeout_seconds=timeout_seconds,
    )
