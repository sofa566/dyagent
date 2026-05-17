from __future__ import annotations

from unittest.mock import patch

from src.models import MCPConnection, RagDataset, SkillEntry


def test_agent_integrations_get_contract_fields(client, db, admin_token, agent):
    # 目的：驗證 integrations 讀取 API 的回傳契約欄位完整。
    # 為什麼：避免前端依賴欄位（如 can_update）在重構後遺失。
    skill = SkillEntry(name='contract-skill', description='for contract', enabled=True)
    mcp = MCPConnection(name='contract-mcp', enabled=True, transport='remote', base_url='https://mcp.example.com')
    dataset = RagDataset(name='contract-global', scope='global', enabled=True)
    db.add_all([skill, mcp, dataset])
    db.commit()
    agent.model_config = {'skill_ids': [str(skill.id)], 'mcp_ids': [str(mcp.id)]}
    agent.rag_config = {'enabled': True, 'sources': ['kb'], 'topK': 5, 'global_dataset_ids': [str(dataset.id)], 'private_dataset_ids': []}
    db.commit()

    response = client.get(f'/api/agents/{agent.id}/integrations', headers={'Authorization': f'Bearer {admin_token}'})

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data.get('mcp_ids'), list)
    assert isinstance(data.get('skill_ids'), list)
    assert isinstance(data.get('rag_config'), dict)
    assert isinstance(data.get('can_update'), bool)
    assert data.get('can_read') is True


def test_agent_integrations_put_contract_fields(client, db, admin_token, agent):
    # 目的：驗證 integrations 寫入後回傳欄位與內容一致。
    # 為什麼：確保前端儲存後可直接用回應更新畫面狀態。
    skill = SkillEntry(name='write-skill', description='for put', enabled=True)
    mcp = MCPConnection(name='write-mcp', enabled=True, transport='remote', base_url='https://mcp.example.com')
    db.add_all([skill, mcp])
    db.commit()

    payload = {
        'mcp_config': [],
        'skills': ['custom-skill'],
        'rag_config': {'enabled': True, 'sources': ['source-a'], 'topK': 7},
        'mcp_ids': [str(mcp.id)],
        'skill_ids': [str(skill.id)],
        'function_profile_id': None,
    }
    response = client.put(f'/api/agents/{agent.id}/integrations', headers={'Authorization': f'Bearer {admin_token}'}, json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data['mcp_ids'] == [str(mcp.id)]
    assert data['skill_ids'] == [str(skill.id)]
    assert data['rag_config']['topK'] == 7


def test_agent_integrations_put_reject_invalid_topk(client, admin_token, agent):
    response = client.put(
        f'/api/agents/{agent.id}/integrations',
        headers={'Authorization': f'Bearer {admin_token}'},
        json={'rag_config': {'enabled': True, 'sources': [], 'topK': 99}},
    )
    assert response.status_code == 400


def test_agent_mcp_test_supports_mcp_id(client, db, admin_token, agent):
    # 目的：驗證 mcp-test 支援以 mcp_id 直接測試。
    # 為什麼：LLM 設定頁綁定 mcp_ids，不應要求前端送完整 connection。
    mcp = MCPConnection(name='id-mcp', enabled=True, transport='remote', base_url='https://mcp.example.com')
    db.add(mcp)
    db.commit()

    with patch('src.api.routes.agents.MCPClient.test_connection', return_value={'ok': True, 'status': 200, 'error': None}):
        response = client.post(
            f'/api/agents/{agent.id}/mcp-test',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'mcp_id': str(mcp.id)},
        )

    assert response.status_code == 200
    assert response.json()['ok'] is True
