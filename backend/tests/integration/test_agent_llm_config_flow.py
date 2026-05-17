from __future__ import annotations

from unittest.mock import patch

from src.models import MCPConnection, RagDataset, SkillEntry


def test_agent_llm_config_save_and_reload_flow(client, db, admin_token, agent):
    # 目的：驗證代理者整合設定的儲存與重載一致性。
    # 為什麼：這是 LLM 設定頁最核心的互動流程，需防止回寫遺漏。
    skill = SkillEntry(name='flow-skill', description='flow', enabled=True)
    mcp = MCPConnection(name='flow-mcp', enabled=True, transport='remote', base_url='https://mcp.flow.test')
    dataset = RagDataset(name='flow-global', scope='global', enabled=True)
    db.add_all([skill, mcp, dataset])
    db.commit()

    save_payload = {
        'mcp_config': [],
        'skills': ['custom-flow-skill'],
        'rag_config': {
            'enabled': True,
            'sources': ['flow-kb'],
            'topK': 6,
            'global_dataset_ids': [str(dataset.id)],
            'private_dataset_ids': [],
        },
        'mcp_ids': [str(mcp.id)],
        'skill_ids': [str(skill.id)],
    }
    save_response = client.put(
        f'/api/agents/{agent.id}/integrations',
        headers={'Authorization': f'Bearer {admin_token}'},
        json=save_payload,
    )
    assert save_response.status_code == 200

    reload_response = client.get(
        f'/api/agents/{agent.id}/integrations',
        headers={'Authorization': f'Bearer {admin_token}'},
    )
    assert reload_response.status_code == 200
    data = reload_response.json()
    assert data['mcp_ids'] == [str(mcp.id)]
    assert data['skill_ids'] == [str(skill.id)]
    assert data['rag_config']['enabled'] is True
    assert data['rag_config']['topK'] == 6


def test_agent_llm_config_mcp_test_and_rag_test_flow(client, db, admin_token, agent):
    # 目的：驗證 MCP 測試與 RAG 測試在設定流程中可獨立成功。
    # 為什麼：設定頁需要先測試再儲存，兩種測試都必須穩定回應。
    mcp = MCPConnection(name='flow-mcp-test', enabled=True, transport='remote', base_url='https://mcp.flow.test')
    db.add(mcp)
    db.commit()

    with patch('src.api.routes.agents.MCPClient.test_connection', return_value={'ok': True, 'status': 200, 'error': None}):
        mcp_test_response = client.post(
            f'/api/agents/{agent.id}/mcp-test',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'mcp_id': str(mcp.id)},
        )
    assert mcp_test_response.status_code == 200
    assert mcp_test_response.json()['ok'] is True

    with (
        patch('src.api.routes.agents.embedding_service.embed_one', return_value=[0.1, 0.2, 0.3]),
        patch(
            'src.api.routes.agents.qdrant_service.search',
            return_value=[
                {
                    'id': 'doc-1',
                    'score': 0.88,
                    'payload': {
                        'document_id': 'doc-1',
                        'snippet': '流程測試片段',
                        'filename': 'flow.md',
                    },
                }
            ],
        ),
    ):
        rag_test_response = client.post(
            f'/api/agents/{agent.id}/rag-test',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={'query': '流程測試', 'sources': ['flow-kb'], 'topK': 5},
        )
    assert rag_test_response.status_code == 200
    rag_data = rag_test_response.json()
    assert rag_data['ok'] is True
    assert len(rag_data['results']) >= 1
