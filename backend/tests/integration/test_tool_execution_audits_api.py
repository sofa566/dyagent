from __future__ import annotations

from datetime import datetime

from src.models import ToolExecutionAudit


def test_list_tool_execution_audits_requires_dashboard_permission(client, regular_user_token):
    response = client.get(
        '/api/admin/tool-execution-audits',
        headers={'Authorization': f'Bearer {regular_user_token}'},
    )
    assert response.status_code == 403


def test_list_tool_execution_audits_returns_filtered_rows(client, db, admin_token, admin_user, agent):
    db.add_all([
        ToolExecutionAudit(
            user_id=admin_user.id,
            agent_id=agent.id,
            conversation_id=None,
            tool_name='mcp:fetch',
            tool_type='mcp',
            risk_level='restricted',
            cost_class='billable',
            allowlist_passed=True,
            confirmation_required=False,
            confirmation_passed=True,
            quota_passed=True,
            status='success',
            deny_reason=None,
            payload_keys=['url'],
            details={'stage': 'after_execute'},
            created_at=datetime.now(),
        ),
        ToolExecutionAudit(
            user_id=admin_user.id,
            agent_id=agent.id,
            conversation_id=None,
            tool_name='dangerous-skill',
            tool_type='skill',
            risk_level='dangerous',
            cost_class='free',
            allowlist_passed=True,
            confirmation_required=True,
            confirmation_passed=False,
            quota_passed=True,
            status='denied',
            deny_reason='confirmation_required',
            payload_keys=['action'],
            details={'stage': 'before_execute'},
            created_at=datetime.now(),
        ),
    ])
    db.commit()

    response = client.get(
        '/api/admin/tool-execution-audits?status=denied&risk_level=dangerous&limit=20',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert response.status_code == 200
    payload = response.json()
    assert int(payload.get('total') or 0) >= 1
    items = payload.get('items') or []
    assert len(items) >= 1
    first_item = items[0]
    assert first_item.get('status') == 'denied'
    assert first_item.get('risk_level') == 'dangerous'
    assert first_item.get('tool_name') == 'dangerous-skill'
