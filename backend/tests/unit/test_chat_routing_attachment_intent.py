from __future__ import annotations

from unittest.mock import patch

from src.api.routes.chat import _extract_routing_message, _pick_worker_agent
from src.models import Agent


def test_extract_routing_message_strips_attachment_block() -> None:
    message = '將這個檔案改成更像人類所寫的。\n\n[附加輸入]\n上傳檔案:\n- travel.docx'
    assert _extract_routing_message(message) == '將這個檔案改成更像人類所寫的。'


def test_pick_worker_agent_uses_primary_message_for_intent_detection(db, workspace) -> None:
    router = Agent(
        name='Router',
        description='主路由',
        model_type='cloud',
        is_router=True,
        agent_class='master',
        enabled=True,
        workspace_id=workspace.id,
    )
    public_worker = Agent(
        name='前瞻研究部',
        description='一般問題回覆',
        model_type='cloud',
        agent_class='public',
        enabled=True,
        workspace_id=workspace.id,
    )
    db.add(router)
    db.add(public_worker)
    db.commit()

    message = '將這個檔案改成更像人類所寫的。\n\n[附加輸入]\n上傳檔案:\n- 詩畫般的仙境：日月潭旅遊全攻略.docx'

    with (
        patch('src.api.routes.chat._extract_agent_skill_names', return_value=[]),
        patch('src.api.routes.chat._pick_worker_without_default', return_value=(public_worker, 'rule_match')),
        patch('src.api.routes.chat._detect_intent_skill_name', return_value=None) as mock_detect,
    ):
        _pick_worker_agent(db=db, router_agent=router, message=message)

    assert mock_detect.call_count == 1
    assert mock_detect.call_args.kwargs['message'] == '將這個檔案改成更像人類所寫的。'
