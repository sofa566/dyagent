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
import tempfile
import subprocess
import shutil
import uuid
from pathlib import Path
from typing import Any
from dataclasses import dataclass

from src.core.config import settings


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
            zf.extractall(work_dir)
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
                   timeout_seconds: int = 30) -> dict[str, Any]:
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
        cmd = ['python', str(script_path)]
    elif ext == '.sh':
        cmd = ['bash', str(script_path)]
    else:
        # 嘗試直接執行
        cmd = [str(script_path)]

    # 準備環境變數
    env = os.environ.copy()
    if input_data:
        import json
        env['SKILL_INPUT'] = json.dumps(input_data, ensure_ascii=False)

    try:
        result = subprocess.run(
            cmd,
            cwd=str(work_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds
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
        # 解壓 ZIP（若有）
        if zip_bundle:
            work_dir = extract_skill_zip(zip_bundle, skill_id)

            # 執行腳本
            if execute_scripts:
                scripts_dir = _find_scripts_dir(work_dir)
                if scripts_dir:
                    # 依檔名排序執行
                    script_files = sorted(
                        [f for f in scripts_dir.iterdir() if f.is_file() and f.suffix.lower() in ('.py', '.sh')],
                        key=lambda x: x.name
                    )
                    for script_file in script_files:
                        output = execute_script(script_file, work_dir, input_data, timeout_seconds)
                        script_outputs.append(output)

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
