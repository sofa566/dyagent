#!/usr/bin/env python3
from __future__ import annotations

import json
import argparse
import re
import sys
import uuid
from pathlib import Path
from typing import Any


def _bootstrap_backend_import_path() -> None:
    project_root = Path(__file__).resolve().parents[1]
    backend_path = project_root / 'backend'
    llm_client_path = backend_path / 'src' / 'services' / 'llm_client.py'
    if llm_client_path.exists() and str(backend_path) not in sys.path:
        sys.path.insert(0, str(backend_path))


def _extract_yaml_scalar(text: str, key: str) -> str:
    pattern = rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$"
    matched = re.search(pattern, text, flags=re.MULTILINE)
    if matched is None:
        return ''
    raw_value = str(matched.group(1) or '').strip()
    if (raw_value.startswith('"') and raw_value.endswith('"')) or (raw_value.startswith("'") and raw_value.endswith("'")):
        return raw_value[1:-1].strip()
    return raw_value


def _sanitize_fixed_description(description: str) -> str:
    candidate = str(description or '').strip()
    invalid_yaml_markers = {'>', '|', '>-', '|-', '>+', '|+'}
    if candidate in invalid_yaml_markers:
        return ''
    return candidate


def _parse_front_matter_yaml(metadata: str) -> dict[str, Any]:
    try:
        import yaml
    except Exception:
        return {}
    try:
        data = yaml.safe_load(metadata) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _infer_description_from_markdown(text: str) -> str:
    candidate_lines = [line.strip() for line in str(text or '').splitlines() if line.strip() and not line.strip().startswith('#')]
    if not candidate_lines:
        return ''
    return candidate_lines[0]


def _extract_system_prompt_from_markdown(skill_md_text: str) -> str:
    # 目的：從 SKILL.md 本文穩定萃取可用的 system_prompt 候選文字。
    # 為什麼：LLM 解析可能回傳 null，需有可預期的本地保底邏輯。
    content = str(skill_md_text or '').strip()
    if not content:
        return ''

    body_match = re.match(r'^---\s*\n.*?\n---\s*\n?(.*)$', content, flags=re.DOTALL)
    body = str(body_match.group(1) or '').strip() if body_match is not None else content
    lines = [line.rstrip() for line in body.splitlines()]

    role_candidates = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith('#') and not re.match(r'^[-*]\s+', line.strip())
    ]
    role_line = ''
    for candidate in role_candidates:
        if '你是' in candidate:
            role_line = candidate
            break
    if not role_line and role_candidates:
        role_line = role_candidates[0]

    numbered_step_lines = [
        line.strip()
        for line in lines
        if re.match(r'^\d+\.\s+', line.strip())
    ]
    selected_steps = numbered_step_lines[:5]

    prompt_parts: list[str] = []
    if role_line:
        prompt_parts.append(role_line)
    if selected_steps:
        prompt_parts.append('請依以下流程執行：')
        prompt_parts.extend(selected_steps)

    prompt = '\n'.join(prompt_parts).strip()
    if not prompt:
        return ''
    return prompt[:1200]


def _extract_skill_md_fixed_fields(skill_md_text: str) -> tuple[str, str]:
    # 目的：穩定萃取 SKILL.md 的固定欄位 name 與 description。
    # 為什麼：固定欄位需優先可信，避免完全依賴 LLM 導致基礎識別不一致。
    content = str(skill_md_text or '').strip()
    if not content:
        return '', ''

    name = ''
    description = ''
    front_matter = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', content, flags=re.DOTALL)
    if front_matter is not None:
        metadata = str(front_matter.group(1) or '')
        body = str(front_matter.group(2) or '').strip()
        front_matter_obj = _parse_front_matter_yaml(metadata)
        if front_matter_obj:
            name = str(front_matter_obj.get('name') or '').strip()
            description = str(front_matter_obj.get('description') or '').strip()
        else:
            name = _extract_yaml_scalar(metadata, 'name')
            description = _extract_yaml_scalar(metadata, 'description')
        if not description and body:
            description = _infer_description_from_markdown(body)

    if not name:
        heading = re.search(r'^#\s+(.+)$', content, flags=re.MULTILINE)
        if heading is not None:
            name = str(heading.group(1) or '').strip()

    if not description:
        description = _infer_description_from_markdown(content)

    return name, _sanitize_fixed_description(description)


def _extract_fixed_system_prompt(skill_md_text: str) -> str:
    # 目的：萃取 SKILL.md 固定 system_prompt，並提供可重現的 fallback。
    # 為什麼：避免模型未輸出 system_prompt 時造成結果欄位為 null 或空值。
    content = str(skill_md_text or '').strip()
    if not content:
        return ''

    front_matter = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', content, flags=re.DOTALL)
    if front_matter is not None:
        metadata = str(front_matter.group(1) or '')
        front_matter_obj = _parse_front_matter_yaml(metadata)
        if front_matter_obj:
            candidate = str(front_matter_obj.get('system_prompt') or '').strip()
            if candidate:
                return candidate
        candidate = _extract_yaml_scalar(metadata, 'system_prompt')
        if candidate:
            return candidate

    return _extract_system_prompt_from_markdown(content)


def _extract_first_json_object(raw_text: str) -> dict[str, Any] | None:
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


def _build_parse_prompt(*, skill_md_text: str, fixed_name: str, fixed_description: str, fixed_system_prompt: str) -> str:
    return (
        '你是技能規格解析器。請將使用者提供的 SKILL.md 轉為單一 JSON 物件。\n'
        '嚴格規則：\n'
        '1) 僅輸出 JSON，不可輸出 markdown 或解釋文字。\n'
        '2) 必須包含 name、description 兩個欄位。\n'
        '3) description 必須是可讀的一句說明，不可是單一符號（例如 >、|）。\n'
        '4) 不可憑空捏造資訊；不確定請填 null 或空陣列。\n'
        '5) 必須包含 system_prompt 欄位，且型別必須是字串，不可為 null。\n'
        '6) 其餘自由內容請盡可能放到 fields（物件）、init（陣列）、steps（陣列）、examples（陣列）、constraints（陣列）。\n'
        '7) name、description、system_prompt 優先使用提供的固定值；若固定值為空，請依 SKILL.md 內容補齊。\n\n'
        f'固定 name：{fixed_name or ""}\n'
        f'固定 description：{fixed_description or ""}\n\n'
        f'固定 system_prompt：{fixed_system_prompt or ""}\n\n'
        '[SKILL.md]\n'
        f'{skill_md_text}\n'
    )


def _normalize_result(*, parsed_json: dict[str, Any], fixed_name: str, fixed_description: str, fixed_system_prompt: str, source_text: str) -> dict[str, Any]:
    # 目的：統一 LLM 輸出為固定欄位結構，避免後續流程分支過多。
    # 為什麼：模型輸出可能缺欄位或型別不一致，需要在落地前做防呆與補齊。
    normalized_json = dict(parsed_json)
    normalized_json['name'] = str(fixed_name or normalized_json.get('name') or '').strip()
    final_description = str(fixed_description or normalized_json.get('description') or '').strip()
    final_description = _sanitize_fixed_description(final_description)
    if not final_description:
        final_description = _infer_description_from_markdown(source_text)
    normalized_json['description'] = final_description
    final_system_prompt = str(fixed_system_prompt or normalized_json.get('system_prompt') or '').strip()
    if not final_system_prompt:
        final_system_prompt = _extract_system_prompt_from_markdown(source_text)
    normalized_json['system_prompt'] = final_system_prompt
    if not isinstance(normalized_json.get('fields'), dict):
        normalized_json['fields'] = {}
    for array_field in ('init', 'steps', 'examples', 'constraints'):
        if not isinstance(normalized_json.get(array_field), list):
            normalized_json[array_field] = []
    normalized_json['source_markdown'] = source_text
    return normalized_json


def _print_route_summary(route_info: dict[str, Any], *, show_route: bool) -> None:
    # 目的：顯示本次實際模型路由摘要。
    # 為什麼：快速確認 provider/model/base_hint，避免誤連到非預期端點。
    provider = str(route_info.get('provider') or '').strip() or 'unknown'
    model = str(route_info.get('model') or '').strip() or 'unknown'
    base_hint = str(route_info.get('base_hint') or '').strip() or 'n/a'
    print(f"[路由] provider={provider} model={model} base_hint={base_hint}", file=sys.stderr)
    if show_route:
        print(f"[路由詳情] {json.dumps(route_info, ensure_ascii=False)}", file=sys.stderr)


def parse_skill_to_json(*, skill_md_path: Path, output_json_path: Path, show_route: bool = False) -> int:
    # 目的：讀取 SKILL.md，呼叫 LLM 解譯後輸出 JSON 檔。
    # 為什麼：將高度自由的技能描述標準化，供系統以機器可讀格式處理。
    if not skill_md_path.exists() or not skill_md_path.is_file():
        print(f"[錯誤] 找不到 SKILL.md: {skill_md_path}", file=sys.stderr)
        return 2

    try:
        source_text = skill_md_path.read_text(encoding='utf-8').strip()
    except Exception as error:
        print(f"[錯誤] 讀取檔案失敗: {error}", file=sys.stderr)
        return 2

    if not source_text:
        print('[錯誤] SKILL.md 內容為空', file=sys.stderr)
        return 2

    fixed_name, fixed_description = _extract_skill_md_fixed_fields(source_text)
    fixed_system_prompt = _extract_fixed_system_prompt(source_text)
    prompt = _build_parse_prompt(
        skill_md_text=source_text,
        fixed_name=fixed_name,
        fixed_description=fixed_description,
        fixed_system_prompt=fixed_system_prompt,
    )

    try:
        _bootstrap_backend_import_path()
        from src.services.llm_client import LLMClient

        llm = LLMClient()
        session_id = f"parse-skill-{uuid.uuid4().hex[:8]}"
        try:
            llm.init_for_session(session_id=session_id)
        except TypeError:
            llm.init_for_session(session_id=session_id, preferred_tier=None)
        raw_output = str(llm.complete(prompt=prompt, tier=None) or '').strip()
        route_info = llm.last_route_info() if hasattr(llm, 'last_route_info') else {}
        if isinstance(route_info, dict):
            _print_route_summary(route_info, show_route=show_route)
    except Exception as error:
        print(f"[錯誤] LLM 呼叫失敗: {error}", file=sys.stderr)
        fallback_json = _normalize_result(
            parsed_json={},
            fixed_name=fixed_name,
            fixed_description=fixed_description,
            fixed_system_prompt=fixed_system_prompt,
            source_text=source_text,
        )
        output_json_path.parent.mkdir(parents=True, exist_ok=True)
        output_json_path.write_text(json.dumps(fallback_json, ensure_ascii=False, indent=2), encoding='utf-8')
        return 1

    parsed_json = _extract_first_json_object(raw_output)
    if parsed_json is None:
        print('[警告] LLM 回覆不是合法 JSON，已輸出 fallback JSON', file=sys.stderr)
        parsed_json = {}

    normalized_json = _normalize_result(
        parsed_json=parsed_json,
        fixed_name=fixed_name,
        fixed_description=fixed_description,
        fixed_system_prompt=fixed_system_prompt,
        source_text=source_text,
    )

    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    output_json_path.write_text(json.dumps(normalized_json, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"[完成] 已輸出 JSON: {output_json_path}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description='將 SKILL.md 透過 LLM 解析為 JSON')
    parser.add_argument('skill_md', help='SKILL.md 路徑')
    parser.add_argument('output_json', help='輸出 JSON 路徑')
    parser.add_argument('--show-route', action='store_true', help='顯示完整路由資訊（llm.last_route_info）')
    parsed_args = parser.parse_args(argv[1:])

    skill_md_path = Path(parsed_args.skill_md).expanduser().resolve()
    output_json_path = Path(parsed_args.output_json).expanduser().resolve()
    return parse_skill_to_json(
        skill_md_path=skill_md_path,
        output_json_path=output_json_path,
        show_route=bool(parsed_args.show_route),
    )


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
