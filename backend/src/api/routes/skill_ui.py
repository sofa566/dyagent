from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import re

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from src.api.errors import not_found_error, validation_error
from src.core.database import get_db
from src.models import Conversation, SkillEntry, SkillInteraction
from src.services.skill_executor import extract_skill_ui_dir, resolve_skill_ui_asset
from src.services.skill_ui_token_service import verify_skill_ui_token


router = APIRouter()


@router.get('/skills/ui/{interaction_id}/{asset_path:path}')
async def serve_skill_ui_asset(
    interaction_id: str,
    asset_path: str,
    request: Request,
    token: str = Query(default=''),
    db: Session = Depends(get_db),
):
    """提供技能 ZIP 中 ui/ 目錄的靜態資源。"""
    effective_token = str(token or '').strip()
    if not effective_token:
        referer_text = str(request.headers.get('referer') or '').strip()
        if referer_text:
            parsed_referer = urlparse(referer_text)
            referer_query = parse_qs(parsed_referer.query)
            effective_token = str((referer_query.get('token') or [''])[0]).strip()

    verified, verify_error, token_payload = verify_skill_ui_token(effective_token)
    if not verified or token_payload is None:
        raise validation_error(f'ui_token_invalid: {verify_error or "unknown"}')

    token_interaction_id = str(token_payload.get('interaction_id') or '')
    if token_interaction_id != str(interaction_id):
        raise validation_error('ui_token_interaction_mismatch')

    interaction_row = db.query(SkillInteraction).filter(SkillInteraction.id == interaction_id).first()
    if interaction_row is None:
        raise not_found_error('SkillInteraction', interaction_id)

    token_conversation_id = str(token_payload.get('conversation_id') or '')
    if str(interaction_row.conversation_id) != token_conversation_id:
        raise validation_error('ui_token_conversation_mismatch')

    token_tool_name = str(token_payload.get('tool_name') or '')
    if str(interaction_row.tool_name or '') != token_tool_name:
        raise validation_error('ui_token_tool_mismatch')

    if str(interaction_row.status or 'active') not in {'active', 'completed'}:
        raise validation_error('interaction_inactive')

    if isinstance(interaction_row.expires_at, datetime) and datetime.now() > interaction_row.expires_at:
        raise validation_error('interaction_expired')

    conversation_row = db.query(Conversation).filter(Conversation.id == interaction_row.conversation_id).first()
    if conversation_row is None:
        raise not_found_error('Conversation', str(interaction_row.conversation_id))

    skill_row = db.query(SkillEntry).filter(SkillEntry.name == interaction_row.tool_name).first()
    if skill_row is None or not bool(skill_row.enabled):
        raise not_found_error('Skill', str(interaction_row.tool_name))
    zip_bundle = getattr(skill_row, 'zip_bundle', None)
    if not zip_bundle:
        raise validation_error('skill_zip_bundle_not_found')

    base_dir = extract_skill_ui_dir(zip_bundle, str(skill_row.id))
    try:
        safe_asset_path = str(Path(asset_path or '').as_posix())
        target_path = resolve_skill_ui_asset(base_dir, safe_asset_path)
    except FileNotFoundError:
        raise not_found_error('SkillUiAsset', asset_path)
    except ValueError as path_error:
        raise validation_error(str(path_error))

    if str(target_path.suffix or '').lower() == '.html':
        html_text = target_path.read_text(encoding='utf-8')
        html_text = _append_token_to_relative_assets(html_text=html_text, token=effective_token)
        return HTMLResponse(content=html_text)

    return FileResponse(path=target_path)


def _append_token_to_relative_assets(*, html_text: str, token: str) -> str:
    # 目的：為 HTML 中相對資源自動補上 token。
    # 為什麼：iframe 內 script/link 二次請求不會自動帶 query token，需顯式附加避免被授權擋下。
    if not token:
        return html_text

    pattern = re.compile(r'(\b(?:src|href)\s*=\s*["\'])(\./[^"\']+)(["\'])', re.IGNORECASE)

    def _replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        raw_path = match.group(2)
        suffix = match.group(3)
        if raw_path.startswith('./'):
            separator = '&' if '?' in raw_path else '?'
            return f"{prefix}{raw_path}{separator}token={token}{suffix}"
        return match.group(0)

    return pattern.sub(_replace, html_text)
