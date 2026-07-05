from __future__ import annotations

import io
from unittest.mock import patch

from src.models import Conversation


def test_tool_injection_splits_multi_attachments(client, db, regular_user, regular_user_token, agent):
    # 目的：驗證多附件在工具執行前會分檔注入 `_attachments`。
    # 為什麼：避免把多份來源合併成單一字串，導致技能無法判斷來源與順序。
    conversation = Conversation(agent_id=agent.id, user_id=regular_user.id)
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    uploaded_ids: list[str] = []
    for name, content in [('a.txt', b'alpha'), ('b.txt', b'beta')]:
        upload_response = client.post(
            '/api/chat/attachments',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            files={'file': (name, io.BytesIO(content), 'text/plain')},
            data={'conversation_id': str(conversation.id)},
        )
        assert upload_response.status_code == 200
        uploaded_ids.append(str(upload_response.json()['attachment']['id']))

    captured_payload: dict = {}

    async def _fake_call_tool_async(self, *, session_id, tool, payload, db, agent_id):
        captured_payload.update(payload or {})
        return {'ok': True}

    with patch('src.api.routes.chat.ChatRouter.call_tool_async', new=_fake_call_tool_async):
        invoke_response = client.post(
            f'/api/conversations/{conversation.id}/tools/mock-tool',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            json={'attachment_ids': uploaded_ids},
        )

    assert invoke_response.status_code == 200
    attachments_payload = captured_payload.get('_attachments')
    assert isinstance(attachments_payload, list)
    assert len(attachments_payload) == 2
    assert [str((row or {}).get('attachment_id') or '') for row in attachments_payload] == uploaded_ids
