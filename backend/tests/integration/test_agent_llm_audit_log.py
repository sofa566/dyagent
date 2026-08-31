from __future__ import annotations


def test_agent_integrations_update_writes_audit_log_and_summary(client, admin_token, agent):
    # 目的：驗證整合設定更新會寫入可追蹤的 before/after 審計資訊。
    # 為什麼：US3 需要管理者可檢視變更摘要，支撐設定追蹤與問題回溯。
    payload = {
        'mcp_config': [],
        'skills': ['audit-skill'],
        'rag_config': {'enabled': True, 'sources': ['audit-kb'], 'topK': 6},
        'mcp_ids': [],
        'skill_ids': [],
        'function_profile_id': None,
        'toolcall_guide': '請依規範使用工具',
    }
    update_response = client.put(
        f'/api/agents/{agent.id}/integrations',
        headers={'Authorization': f'Bearer {admin_token}'},
        json=payload,
    )
    assert update_response.status_code == 200

    audit_response = client.get(
        f'/api/agents/{agent.id}/integrations/audit',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert audit_response.status_code == 200

    entries = audit_response.json().get('entries') or []
    assert len(entries) >= 1
    latest_entry = entries[0]
    details = latest_entry.get('details') or {}
    assert isinstance(details.get('before'), dict)
    assert isinstance(details.get('after'), dict)
    assert 'changed_fields' in details
    assert 'toolcall_guide' in (details.get('changed_fields') or [])


def test_non_admin_cannot_read_agent_integrations_audit(client, regular_user_token, agent):
    response = client.get(
        f'/api/agents/{agent.id}/integrations/audit',
        headers={'Authorization': f'Bearer {regular_user_token}'},
    )
    assert response.status_code == 403
