from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

from jose import JWTError, jwt
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error, not_found_error, validation_error
from src.core.config import settings
from src.models import PatientPortalLink, RenalPatient

PATIENT_PORTAL_LINK_TTL_SECONDS_DEFAULT = 600
PATIENT_PORTAL_LINK_TTL_SECONDS_MIN = 60
PATIENT_PORTAL_LINK_TTL_SECONDS_MAX = 1800


class PatientPortalAuthService:
    # 目的：管理腎友 LINE 免註冊 magic link 的簽發、驗證與撤銷。
    # 為什麼：POC 需讓腎友免註冊登入，同時保留最小可用安全控制。

    def request_magic_link(self, *, db: Session, patient_code: str, line_user_id: str, ttl_seconds: int, reason: str, created_by_user_id: str | None) -> dict:
        # 目的：建立一次性短效登入連結並保存可撤銷狀態。
        # 為什麼：僅靠靜態連結風險高，需有到期、撤銷與審計能力。
        patient = self._get_patient_by_code(db=db, patient_code=patient_code)
        normalized_line_user_id = str(line_user_id or '').strip()
        if not normalized_line_user_id:
            raise validation_error('line_user_id 不可為空')
        if str(getattr(patient, 'line_user_id', '') or '').strip() != normalized_line_user_id:
            raise validation_error('病患與 LINE 使用者映射不一致')

        safe_ttl = self._safe_ttl(ttl_seconds)
        expires_at = datetime.now() + timedelta(seconds=safe_ttl)
        link_id = str(uuid.uuid4())
        token = self._build_magic_token(patient_code=patient.patient_code, line_user_id=normalized_line_user_id, link_id=link_id, expires_at=expires_at)
        token_hash = self._hash_token(token)

        link_row = PatientPortalLink(
            id=uuid.UUID(link_id),
            patient_id=patient.id,
            line_user_id=normalized_line_user_id,
            token_hash=token_hash,
            reason=str(reason or '').strip() or None,
            expires_at=expires_at,
            created_by_user_id=created_by_user_id,
        )
        db.add(link_row)
        db.commit()
        return {
            'ok': True,
            'link_id': link_id,
            'expires_at': expires_at.isoformat(),
            'magic_link': f'/pages/renal-care-patient.html?token={token}',
        }

    def login_with_magic_token(self, *, db: Session, token: str) -> dict:
        # 目的：驗證 magic token 並簽發病患工作台 session token。
        # 為什麼：腎友需免註冊進入網頁，同時限制一次性使用與過期登入。
        normalized_token = str(token or '').strip()
        if not normalized_token:
            raise validation_error('token 不可為空')

        payload = self._decode_token(normalized_token)
        if str(payload.get('scope') or '') != 'patient_portal_login':
            raise forbidden_error('token scope 無效')
        link_id = str(payload.get('link_id') or '').strip()
        if not link_id:
            raise forbidden_error('token 缺少 link_id')

        row = db.query(PatientPortalLink).filter(PatientPortalLink.id == link_id).first()
        if row is None:
            raise not_found_error('PatientPortalLink', link_id)
        if row.revoked_at is not None:
            raise forbidden_error('登入連結已撤銷')
        if row.used_at is not None:
            raise forbidden_error('登入連結已使用')
        if row.expires_at <= datetime.now():
            raise forbidden_error('登入連結已過期')
        if row.token_hash != self._hash_token(normalized_token):
            raise forbidden_error('登入連結驗證失敗')

        patient = db.query(RenalPatient).filter(RenalPatient.id == row.patient_id, RenalPatient.enabled == True).first()  # noqa: E712
        if patient is None:
            raise not_found_error('RenalPatient', str(row.patient_id))

        row.used_at = datetime.now()
        db.add(row)
        db.commit()

        session_expires_at = datetime.now() + timedelta(minutes=60)
        session_token = jwt.encode(
            {
                'sub': f'patient:{patient.patient_code}',
                'scope': 'patient_portal_session',
                'patient_id': patient.patient_code,
                'line_user_id': row.line_user_id,
                'exp': session_expires_at,
            },
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM,
        )
        return {
            'ok': True,
            'session_token': session_token,
            'expires_at': session_expires_at.isoformat(),
            'patient': {'id': str(patient.patient_code), 'display_name': str(patient.display_name or patient.patient_code)},
        }

    def resend_magic_link(self, *, db: Session, patient_code: str, reason: str, created_by_user_id: str | None) -> dict:
        # 目的：撤銷舊連結並重發新連結。
        # 為什麼：連結過期或使用者誤刪時，需要可操作的恢復流程。
        patient = self._get_patient_by_code(db=db, patient_code=patient_code)
        if not str(getattr(patient, 'line_user_id', '') or '').strip():
            raise validation_error('該病患尚未綁定 LINE 使用者')
        self.revoke_all_patient_links(db=db, patient_id=str(patient.id), reason='resend')
        response = self.request_magic_link(
            db=db,
            patient_code=patient.patient_code,
            line_user_id=str(patient.line_user_id),
            ttl_seconds=PATIENT_PORTAL_LINK_TTL_SECONDS_DEFAULT,
            reason=reason,
            created_by_user_id=created_by_user_id,
        )
        return {'ok': True, 'new_link_id': response['link_id'], 'expires_at': response['expires_at'], 'magic_link': response['magic_link']}

    def revoke_link(self, *, db: Session, link_id: str) -> dict:
        if not link_id:
            raise validation_error('link_id 不可為空')
        row = db.query(PatientPortalLink).filter(PatientPortalLink.id == link_id).first()
        if row is None:
            raise not_found_error('PatientPortalLink', link_id)
        row.revoked_at = datetime.now()
        db.add(row)
        db.commit()
        return {'ok': True, 'revoked': True}

    def revoke_all_patient_links(self, *, db: Session, patient_id: str, reason: str) -> None:
        _ = reason
        rows = db.query(PatientPortalLink).filter(PatientPortalLink.patient_id == patient_id, PatientPortalLink.revoked_at == None).all()  # noqa: E711
        now = datetime.now()
        for row in rows:
            row.revoked_at = now
            db.add(row)
        db.flush()

    def _get_patient_by_code(self, *, db: Session, patient_code: str) -> RenalPatient:
        if not patient_code:
            raise validation_error('patient_id 不可為空')
        row = db.query(RenalPatient).filter(RenalPatient.patient_code == patient_code, RenalPatient.enabled == True).first()  # noqa: E712
        if row is None:
            raise not_found_error('RenalPatient', patient_code)
        return row

    def _safe_ttl(self, ttl_seconds: int) -> int:
        try:
            ttl = int(ttl_seconds or PATIENT_PORTAL_LINK_TTL_SECONDS_DEFAULT)
        except Exception:
            ttl = PATIENT_PORTAL_LINK_TTL_SECONDS_DEFAULT
        return max(PATIENT_PORTAL_LINK_TTL_SECONDS_MIN, min(ttl, PATIENT_PORTAL_LINK_TTL_SECONDS_MAX))

    def _build_magic_token(self, *, patient_code: str, line_user_id: str, link_id: str, expires_at: datetime) -> str:
        return jwt.encode(
            {
                'sub': f'patient:{patient_code}',
                'scope': 'patient_portal_login',
                'patient_id': patient_code,
                'line_user_id': line_user_id,
                'link_id': link_id,
                'exp': expires_at,
            },
            settings.SECRET_KEY,
            algorithm=settings.ALGORITHM,
        )

    def _decode_token(self, token: str) -> dict:
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            if not isinstance(payload, dict):
                raise forbidden_error('token 內容無效')
            return payload
        except JWTError as error:
            raise forbidden_error(f'token 驗證失敗: {error}') from error

    def decode_patient_session_token(self, *, token: str) -> dict:
        payload = self._decode_token(token)
        if str(payload.get('scope') or '') != 'patient_portal_session':
            raise forbidden_error('病患工作台 session token 無效')
        patient_id = str(payload.get('patient_id') or '').strip()
        if not patient_id:
            raise forbidden_error('病患工作台 session token 缺少 patient_id')
        return payload

    def _hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode('utf-8')).hexdigest()


patient_portal_auth_service = PatientPortalAuthService()
