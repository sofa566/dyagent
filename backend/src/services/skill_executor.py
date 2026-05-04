"""Skill 執行服務。

負責執行 Claude Skills 格式的技能：
1. 解壓 ZIP 到暫存目錄
2. 執行 scripts/ 下的腳本
3. 合併 prompt_template 與腳本輸出

此服務使用 DATA_CACHE_PATH 作為暫存目錄。
"""

import os
import zipfile
import io
import subprocess
import shutil
import uuid
import sys
import hashlib
from pathlib import Path
from typing import Any
from dataclasses import dataclass

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
    except zipfile.BadZipFile as e:
        raise ValueError(f"無效的 ZIP 檔案: {e}")

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
    if not isinstance(input_data, dict):
        return None, [], {}
    script_name = input_data.get('_script')
    script_name_str = str(script_name).strip() if isinstance(script_name, str) and script_name.strip() else None
    raw_args = input_data.get('_args')
    args: list[str] = []
    if isinstance(raw_args, list):
        args = [str(x) for x in raw_args]
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

def execute_skill(
    skill_id: str,
    zip_bundle: bytes | None,
    prompt_template: str | None,
    input_data: dict[str, Any] | None = None,
    execute_scripts: bool = True,
    timeout_seconds: int = 30,
) -> SkillExecutionResult:
    """執行 Claude Skill。

    流程：
    1. 若有 zip_bundle，解壓到暫存目錄
    2. 若有 scripts/ 且 execute_scripts=True，依序執行腳本
    3. 合併 prompt_template 與腳本輸出

    Args:
        skill_id: 技能 ID
        zip_bundle: ZIP 檔案內容（可選）
        prompt_template: 提示詞模板（可選）
        input_data: 傳入腳本的輸入資料
        execute_scripts: 是否執行腳本
        timeout_seconds: 單一腳本逾時秒數

    Returns:
        SkillExecutionResult
    """
    work_dir = None
    script_outputs: list[dict[str, Any]] = []

    try:
        _log.debug("Starting skill execution", skill_id=skill_id, has_zip_bundle=bool(zip_bundle), prompt_template_present=bool(prompt_template), input_data_keys=list(input_data.keys()) if input_data else None, execute_scripts=execute_scripts, timeout_seconds=timeout_seconds)

        script_name, script_args, pure_input_data = _extract_script_controls(input_data)
        _log.debug(f"Extracted script controls: script_name={script_name}, script_args={script_args}, pure_input_data_keys={list(pure_input_data.keys()) if pure_input_data else None}, input_data={input_data}")

        # 解壓 ZIP（若有）
        if zip_bundle:
            work_dir = extract_skill_zip(zip_bundle, skill_id)

            # 執行腳本
            if execute_scripts:
                scripts_dir = _find_scripts_dir(work_dir)
                if scripts_dir:
                    entry_script = _resolve_entry_script(scripts_dir, script_name)
                    if script_name and entry_script is None:
                        _log.error("Entry script not found", script_name=script_name, scripts_dir=scripts_dir)
                        return SkillExecutionResult(
                            ok=False,
                            error=f'script_not_found: {script_name}',
                            work_dir=str(work_dir) if work_dir else None,
                        )
                    if entry_script is not None:
                        runtime_work_dir = entry_script.parent.parent if entry_script.parent.name == 'scripts' else work_dir
                        output = execute_script(
                            entry_script,
                            runtime_work_dir,
                            pure_input_data,
                            timeout_seconds,
                            extra_args=script_args,
                        )
                        script_outputs.append(output)

        _log.debug("Finished script execution", script_outputs=script_outputs)
        # 組合最終提示詞
        final_prompt = prompt_template or ''

        # 若有腳本輸出，附加到提示詞
        if script_outputs:
            outputs_text = '\n\n--- Script Outputs ---\n'
            for out in script_outputs:
                outputs_text += f"\n[{out['script']}]\n"
                if out.get('stdout'):
                    outputs_text += out['stdout']
                if out.get('stderr') and not out.get('ok'):
                    outputs_text += f"\n[Error] {out['stderr']}"
            final_prompt += outputs_text

        return SkillExecutionResult(
            ok=True,
            prompt=final_prompt,
            script_outputs=script_outputs if script_outputs else None,
            work_dir=str(work_dir) if work_dir else None,
        )

    except Exception as e:
        return SkillExecutionResult(
            ok=False,
            error=str(e),
            work_dir=str(work_dir) if work_dir else None,
        )

    finally:
        # 清理（若不需保留）
        # 暫時保留，讓呼叫端決定何時清理
        pass
