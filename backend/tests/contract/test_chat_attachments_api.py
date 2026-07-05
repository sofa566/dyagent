from __future__ import annotations

import io
from datetime import datetime
from unittest.mock import patch

from src.models import ChatAttachment, Conversation


def test_chat_attachment_upload_list_and_get(client, regular_user_token):
    # 目的：驗證聊天附件上傳、列表與單筆查詢契約。
    # 為什麼：前端改為先上傳 binary 再送 attachment_id，需確保三個端點可串接。
    upload_response = client.post(
        '/api/chat/attachments',
        headers={'Authorization': f'Bearer {regular_user_token}'},
        files={'file': ('hello.txt', io.BytesIO(b'hello from test'), 'text/plain')},
    )

    assert upload_response.status_code == 200
    upload_data = upload_response.json()
    assert upload_data.get('ok') is True
    attachment = upload_data.get('attachment') or {}
    attachment_id = str(attachment.get('id') or '')
    assert attachment_id
    assert attachment.get('filename') == 'hello.txt'
    assert attachment.get('status') == 'uploaded'

    list_response = client.get(
        '/api/chat/attachments',
        headers={'Authorization': f'Bearer {regular_user_token}'},
    )
    assert list_response.status_code == 200
    listed = list_response.json().get('attachments') or []
    assert any(str(row.get('id') or '') == attachment_id for row in listed)

    get_response = client.get(
        f'/api/chat/attachments/{attachment_id}',
        headers={'Authorization': f'Bearer {regular_user_token}'},
    )
    assert get_response.status_code == 200
    got_attachment = (get_response.json() or {}).get('attachment') or {}
    assert str(got_attachment.get('id') or '') == attachment_id


def test_chat_attachment_get_not_found(client, regular_user_token):
    response = client.get(
        '/api/chat/attachments/00000000-0000-0000-0000-000000000000',
        headers={'Authorization': f'Bearer {regular_user_token}'},
    )
    assert response.status_code == 404


def test_invoke_tool_auto_includes_conversation_attachments(client, db, regular_user, regular_user_token, agent):
    # 目的：驗證工具執行在未顯式帶 attachment_ids 時，仍會自動注入會話已綁定附件。
    # 為什麼：技能 UI 二次提交通常只送 form_data，不能要求前端每次重送 attachment_ids。
    conversation = Conversation(agent_id=agent.id, user_id=regular_user.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    attachment = ChatAttachment(
        conversation_id=conversation.id,
        user_id=regular_user.id,
        filename='note.txt',
        ext='txt',
        mime_type='text/plain',
        file_path='/tmp/not-found-note.txt',
        size_bytes=12,
        status='uploaded',
        created_at=datetime.now(),
    )
    db.add(attachment)
    db.commit()

    captured_payload: dict = {}

    async def _fake_call_tool_async(self, *, session_id, tool, payload, db, agent_id):
        captured_payload.update(payload or {})
        return {'ok': True}

    with patch('src.api.routes.chat.ChatRouter.call_tool_async', new=_fake_call_tool_async):
        response = client.post(
            f'/api/conversations/{conversation.id}/tools/mock-tool',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            json={},
        )

    assert response.status_code == 200
    attachments = captured_payload.get('_attachments')
    assert isinstance(attachments, list)
    assert len(attachments) == 1
    assert str((attachments[0] or {}).get('attachment_id') or '') == str(attachment.id)
