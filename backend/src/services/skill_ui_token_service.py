from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import base64
import hashlib
import hmac
import json

from src.core.config import settings


def create_skill_ui_token(*, conversation_id: str, tool_name: str, interaction_id: str, expires_in_seconds: int | None = None) -> str:
    # 目的：建立技能 UI 存取用短效簽章 token。
    # 為什麼：避免直接暴露可猜測的靜態資源路徑，並限制 token 使用時效。
    ttl_seconds = int(expires_in_seconds or settings.SKILL_UI_TOKEN_TTL_SEC or 600)
    ttl_seconds = max(60, ttl_seconds)
    expires_at = datetime.now() + timedelta(seconds=ttl_seconds)
    payload = {
        'conversation_id': str(conversation_id),
        'tool_name': str(tool_name),
        'interaction_id': str(interaction_id),
        'exp': int(expires_at.timestamp()),
    }
    return _encode_token(payload)


def verify_skill_ui_token(token_text: str) -> tuple[bool, str | None, dict[str, Any] | None]:
    # 目的：驗證技能 UI token 的簽章與有效期。
    # 為什麼：路由需在讀檔前先完成授權驗證，防止未授權讀取 ZIP 內容。
    token = str(token_text or '').strip()
    if not token:
        return False, 'missing_token', None
    parts = token.split('.', 1)
    if len(parts) != 2:
        return False, 'invalid_token_format', None
    payload_part, signature_part = parts
    expected_sig = _sign(payload_part)
    if not hmac.compare_digest(expected_sig, signature_part):
        return False, 'invalid_token_signature', None
    try:
        payload_json = _urlsafe_b64decode(payload_part)
        payload = json.loads(payload_json)
    except Exception:
        return False, 'invalid_token_payload', None
    if not isinstance(payload, dict):
        return False, 'invalid_token_payload', None
    exp_raw = payload.get('exp')
    if not isinstance(exp_raw, int):
        return False, 'invalid_token_exp', None
    if datetime.now().timestamp() > float(exp_raw):
        return False, 'token_expired', None
    return True, None, payload


def _encode_token(payload: dict[str, Any]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    payload_b64 = _urlsafe_b64encode(payload_json)
    signature = _sign(payload_b64)
    return f'{payload_b64}.{signature}'


def _sign(payload_b64: str) -> str:
    secret_text = str(settings.SKILL_UI_TOKEN_SECRET or '')
    secret_bytes = secret_text.encode('utf-8')
    digest = hmac.new(secret_bytes, payload_b64.encode('utf-8'), hashlib.sha256).digest()
    return _urlsafe_b64encode_bytes(digest)


def _urlsafe_b64encode(raw_text: str) -> str:
    return _urlsafe_b64encode_bytes(raw_text.encode('utf-8'))


def _urlsafe_b64encode_bytes(raw_bytes: bytes) -> str:
    return base64.urlsafe_b64encode(raw_bytes).decode('ascii').rstrip('=')


def _urlsafe_b64decode(encoded: str) -> str:
    padding = '=' * (-len(encoded) % 4)
    raw_bytes = base64.urlsafe_b64decode((encoded + padding).encode('ascii'))
    return raw_bytes.decode('utf-8')
