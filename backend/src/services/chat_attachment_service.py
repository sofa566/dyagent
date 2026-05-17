from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.logging import get_logger
from src.models import ChatAttachment, Conversation
from src.tools.document_convert import convert_document_to_markdown

_log = get_logger('chat_attachment_service')


@dataclass
class AttachmentMarkdown:
    attachment_id: str
    filename: str
    markdown: str
    error: str | None


class ChatAttachmentService:
    """目的：提供聊天附件上傳、查詢與 markdown 轉換能力。
    為什麼：讓附件處理流程與 chat route 解耦，避免路由層承擔檔案 I/O 細節。
    """

    def __init__(self) -> None:
        self._base_dir = Path(settings.DATA_CACHE_PATH) / 'chat-attachments'
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def create_attachment(
        self,
        *,
        db: Session,
        user_id: str,
        file: UploadFile,
        conversation_id: str | None = None,
        ttl_hours: int = 24,
    ) -> ChatAttachment:
        # 目的：接收二進位附件並落地到快取，建立 metadata 記錄。
        # 為什麼：聊天訊息只需攜帶 attachment_id，後續工具執行時再按需讀檔轉換。
        filename = str(getattr(file, 'filename', '') or '').strip()
        if not filename:
            raise ValueError('missing_filename')

        safe_name = self._safe_filename(filename)
        ext = Path(safe_name).suffix.lower().lstrip('.')
        attachment_id = str(uuid.uuid4())
        now = datetime.now()
        expires_at = now + timedelta(hours=max(1, int(ttl_hours or 24)))

        target_dir = self._base_dir / str(user_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f'{attachment_id}_{safe_name}'

        size_bytes = 0
        try:
            with open(target_path, 'wb') as output:
                src = getattr(file, 'file', None)
                if src is None:
                    raise ValueError('missing_file_stream')
                shutil.copyfileobj(src, output)
            size_bytes = int(target_path.stat().st_size)
        except Exception as error:
            if target_path.exists():
                target_path.unlink(missing_ok=True)
            raise ValueError(f'attachment_save_failed:{error}') from error

        row = ChatAttachment(
            id=attachment_id,
            conversation_id=conversation_id,
            user_id=user_id,
            filename=filename,
            ext=ext,
            mime_type=str(getattr(file, 'content_type', '') or 'application/octet-stream'),
            file_path=str(target_path),
            size_bytes=size_bytes,
            status='uploaded',
            error=None,
            created_at=now,
            expires_at=expires_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    def list_user_attachments(
        self,
        *,
        db: Session,
        user_id: str,
        conversation_id: str | None = None,
    ) -> list[ChatAttachment]:
        query = db.query(ChatAttachment).filter(ChatAttachment.user_id == user_id)
        if conversation_id:
            query = query.filter(ChatAttachment.conversation_id == conversation_id)
        return query.order_by(ChatAttachment.created_at.desc()).all()

    def get_user_attachment(self, *, db: Session, user_id: str, attachment_id: str) -> ChatAttachment | None:
        return db.query(ChatAttachment).filter(ChatAttachment.id == attachment_id, ChatAttachment.user_id == user_id).first()

    def bind_attachments_to_conversation(
        self,
        *,
        db: Session,
        user_id: str,
        attachment_ids: list[str],
        conversation: Conversation,
    ) -> list[ChatAttachment]:
        # 目的：將聊天請求攜帶的附件綁定到會話。
        # 為什麼：附件可能先上傳後對話，需在聊天建立會話後補綁關聯以便追蹤。
        rows: list[ChatAttachment] = []
        for attachment_id in attachment_ids:
            row = self.get_user_attachment(db=db, user_id=user_id, attachment_id=attachment_id)
            if row is None:
                continue
            if row.conversation_id is None:
                row.conversation_id = conversation.id
            rows.append(row)
        if rows:
            db.commit()
        return rows

    def build_markdown_bundle(
        self,
        *,
        db: Session,
        user_id: str,
        attachment_ids: list[str],
    ) -> list[AttachmentMarkdown]:
        # 目的：把 attachment_ids 逐檔轉換為 markdown bundle。
        # 為什麼：多附件需分開轉換與注入，禁止合併成單一來源造成上下文混淆。
        bundles: list[AttachmentMarkdown] = []
        for attachment_id in attachment_ids:
            row = self.get_user_attachment(db=db, user_id=user_id, attachment_id=attachment_id)
            if row is None:
                bundles.append(AttachmentMarkdown(attachment_id=attachment_id, filename='', markdown='', error='attachment_not_found'))
                continue
            markdown, error = convert_document_to_markdown(row.file_path)
            row.status = 'converted' if not error else 'failed'
            row.error = error
            bundles.append(
                AttachmentMarkdown(
                    attachment_id=str(row.id),
                    filename=str(row.filename or ''),
                    markdown=markdown,
                    error=error,
                )
            )
        if bundles:
            db.commit()
        return bundles

    def serialize_attachment(self, row: ChatAttachment) -> dict[str, Any]:
        return {
            'id': str(row.id),
            'conversation_id': str(row.conversation_id) if row.conversation_id else None,
            'user_id': str(row.user_id),
            'filename': str(row.filename or ''),
            'ext': str(row.ext or ''),
            'mime_type': str(row.mime_type or ''),
            'size_bytes': int(row.size_bytes or 0),
            'status': str(row.status or 'uploaded'),
            'error': str(row.error or '') if row.error else None,
            'created_at': row.created_at.isoformat() if row.created_at else None,
            'expires_at': row.expires_at.isoformat() if row.expires_at else None,
        }

    def _safe_filename(self, filename: str) -> str:
        cleaned = ''.join(ch for ch in str(filename or '') if ch.isalnum() or ch in {'-', '_', '.', ' '}).strip()
        return cleaned or 'attachment.bin'


chat_attachment_service = ChatAttachmentService()
