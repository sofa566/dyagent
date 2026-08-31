from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from src.models import AccessGroup, Agent, Conversation, LlmTurn, UserGroupBinding


def _create_turn(
    *,
    db,
    agent_id,
    user_id,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    created_at: datetime,
):
    conversation = Conversation(
        user_id=user_id,
        agent_id=agent_id,
        title='cost-test',
        created_at=created_at,
        last_interacted_at=created_at,
    )
    db.add(conversation)
    db.flush()
    db.add(
        LlmTurn(
            conversation_id=conversation.id,
            agent_id=agent_id,
            usage={
                'input_tokens': input_tokens,
                'output_tokens': output_tokens,
                'total_tokens': input_tokens + output_tokens,
                'raw': {},
            },
            cost_usd=cost_usd,
            status='success',
            created_at=created_at,
        )
    )


class TestCostUsageApi:
    def test_admin_cost_overview_returns_company_totals(self, client, db, admin_token, regular_user, workspace):
        now = datetime.now()
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        db.add(first_agent)
        db.flush()

        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=100,
            output_tokens=50,
            cost_usd=Decimal('0.123456'),
            created_at=now,
        )
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=40,
            output_tokens=10,
            cost_usd=Decimal('0.020000'),
            created_at=now,
        )
        db.commit()

        response = client.get('/api/admin/cost/overview?days=7', headers={'Authorization': f'Bearer {admin_token}'})
        assert response.status_code == 200
        payload = response.json()
        assert payload['company']['turns'] == 2
        assert payload['company']['input_tokens'] == 140
        assert payload['company']['output_tokens'] == 60
        assert payload['company']['total_tokens'] == 200
        assert float(payload['company']['cost_usd']) == 0.143456

    def test_admin_cost_breakdown_group_counts_full_usage_for_each_group(self, client, db, admin_token, regular_user, workspace):
        now = datetime.now()
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        first_group = AccessGroup(code='ops_a', name='Ops A', enabled=True)
        second_group = AccessGroup(code='ops_b', name='Ops B', enabled=True)
        db.add_all([first_agent, first_group, second_group])
        db.flush()

        db.add_all(
            [
                UserGroupBinding(user_id=regular_user.id, group_id=first_group.id),
                UserGroupBinding(user_id=regular_user.id, group_id=second_group.id),
            ]
        )
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=20,
            output_tokens=30,
            cost_usd=Decimal('0.050000'),
            created_at=now,
        )
        db.commit()

        response = client.get(
            '/api/admin/cost/breakdown?dimension=group&days=7&limit=10',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['dimension'] == 'group'
        assert payload['total'] == 2
        assert len(payload['items']) == 2
        for row in payload['items']:
            assert row['turns'] == 1
            assert row['input_tokens'] == 20
            assert row['output_tokens'] == 30
            assert row['total_tokens'] == 50
            assert float(row['cost_usd']) == 0.05

    def test_admin_cost_breakdown_user_returns_username(self, client, db, admin_token, regular_user, workspace):
        now = datetime.now()
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        db.add(first_agent)
        db.flush()
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=11,
            output_tokens=9,
            cost_usd=Decimal('0.010000'),
            created_at=now,
        )
        db.commit()

        response = client.get(
            '/api/admin/cost/breakdown?dimension=user&days=7&limit=10',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload['dimension'] == 'user'
        assert payload['total'] == 1
        assert payload['items'][0]['user_id'] == str(regular_user.id)
        assert payload['items'][0]['username'] == regular_user.username

    def test_me_cost_usage_only_counts_selected_month(self, client, db, regular_user_token, regular_user, workspace):
        now = datetime.now()
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        db.add(first_agent)
        db.flush()

        current_month_turn_at = datetime(now.year, now.month, 5, 10, 0, 0)
        previous_month_anchor = datetime(now.year - 1, 12, 5, 10, 0, 0) if now.month == 1 else datetime(now.year, now.month - 1, 5, 10, 0, 0)
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=10,
            output_tokens=20,
            cost_usd=Decimal('0.010000'),
            created_at=current_month_turn_at,
        )
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=999,
            output_tokens=1,
            cost_usd=Decimal('9.990000'),
            created_at=previous_month_anchor,
        )
        db.commit()

        response = client.get('/api/me/cost/usage', headers={'Authorization': f'Bearer {regular_user_token}'})
        assert response.status_code == 200
        payload = response.json()
        usage = payload['usage']
        assert usage['input_tokens'] == 10
        assert usage['output_tokens'] == 20
        assert usage['total_tokens'] == 30
        assert float(usage['cost_usd']) == 0.01

    def test_put_and_get_admin_cost_policies(self, client, db, admin_token, regular_user, workspace):
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        first_group = AccessGroup(code='ops_x', name='Ops X', enabled=True)
        db.add_all([first_agent, first_group])
        db.flush()

        payload = {
            'policies': [
                {
                    'scope_type': 'company',
                    'monthly_total_tokens_limit': 100000,
                    'monthly_cost_usd_limit': 100.5,
                    'warn_thresholds': [50, 80, 100],
                },
                {
                    'scope_type': 'user',
                    'scope_id': str(regular_user.id),
                    'monthly_input_tokens_limit': 5000,
                    'enforcement_mode': 'warn_only',
                    'warn_thresholds': [60, 90],
                },
                {
                    'scope_type': 'group',
                    'scope_id': str(first_group.id),
                    'monthly_total_tokens_limit': 7000,
                    'enforcement_mode': 'hard_limit',
                },
                {
                    'scope_type': 'agent',
                    'scope_id': str(first_agent.id),
                    'monthly_cost_usd_limit': 12.34,
                },
            ]
        }

        put_response = client.put('/api/admin/cost/policies', headers={'Authorization': f'Bearer {admin_token}'}, json=payload)
        assert put_response.status_code == 200
        assert len(put_response.json()['items']) == 4

        get_response = client.get('/api/admin/cost/policies', headers={'Authorization': f'Bearer {admin_token}'})
        assert get_response.status_code == 200
        items = get_response.json()['items']
        assert len(items) == 4
        scope_types = {row['scope_type'] for row in items}
        assert scope_types == {'company', 'user', 'group', 'agent'}

    def test_evaluate_alerts_creates_warned_event_and_dedupes(self, client, db, admin_token, regular_user, workspace):
        now = datetime.now()
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        db.add(first_agent)
        db.flush()
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=120,
            output_tokens=30,
            cost_usd=Decimal('0.250000'),
            created_at=now,
        )
        db.commit()

        policy_response = client.put(
            '/api/admin/cost/policies',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'policies': [
                    {
                        'scope_type': 'company',
                        'monthly_input_tokens_limit': 100,
                        'warn_thresholds': [50, 100],
                        'enforcement_mode': 'warn_only',
                    }
                ]
            },
        )
        assert policy_response.status_code == 200

        first_eval = client.post('/api/admin/cost/alerts/evaluate', headers={'Authorization': f'Bearer {admin_token}'})
        assert first_eval.status_code == 200
        assert first_eval.json()['created_count'] >= 2

        second_eval = client.post('/api/admin/cost/alerts/evaluate', headers={'Authorization': f'Bearer {admin_token}'})
        assert second_eval.status_code == 200
        assert second_eval.json()['created_count'] == 0

        list_response = client.get('/api/admin/cost/alerts', headers={'Authorization': f'Bearer {admin_token}'})
        assert list_response.status_code == 200
        alert_items = list_response.json()['items']
        assert len(alert_items) >= 2
        assert all(item['status'] == 'warned' for item in alert_items)

    def test_hard_limit_policy_marks_100_percent_alert_as_blocked(self, client, db, admin_token, regular_user, workspace):
        now = datetime.now()
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        db.add(first_agent)
        db.flush()
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=100,
            output_tokens=1,
            cost_usd=Decimal('0.050000'),
            created_at=now,
        )
        db.commit()

        policy_response = client.put(
            '/api/admin/cost/policies',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'policies': [
                    {
                        'scope_type': 'company',
                        'monthly_input_tokens_limit': 100,
                        'warn_thresholds': [100],
                        'enforcement_mode': 'hard_limit',
                    }
                ]
            },
        )
        assert policy_response.status_code == 200

        eval_response = client.post('/api/admin/cost/alerts/evaluate', headers={'Authorization': f'Bearer {admin_token}'})
        assert eval_response.status_code == 200
        created_items = eval_response.json()['created_items']
        assert len(created_items) == 1
        assert created_items[0]['status'] == 'blocked'

    def test_chat_stream_denied_when_hard_limit_already_reached(self, client, db, admin_token, regular_user, regular_user_token, workspace):
        now = datetime.now()
        first_agent = Agent(name='A1', description='A1', model_type='cloud', workspace_id=workspace.id)
        db.add(first_agent)
        db.flush()
        _create_turn(
            db=db,
            agent_id=first_agent.id,
            user_id=regular_user.id,
            input_tokens=100,
            output_tokens=1,
            cost_usd=Decimal('0.050000'),
            created_at=now,
        )
        db.commit()

        policy_response = client.put(
            '/api/admin/cost/policies',
            headers={'Authorization': f'Bearer {admin_token}'},
            json={
                'policies': [
                    {
                        'scope_type': 'company',
                        'monthly_input_tokens_limit': 100,
                        'enforcement_mode': 'hard_limit',
                        'warn_thresholds': [100],
                    }
                ]
            },
        )
        assert policy_response.status_code == 200

        response = client.get(
            f'/api/agents/{first_agent.id}/chat/stream',
            params={'message': '你好'},
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )
        assert response.status_code == 200
        assert 'llm.cost.denied' not in response.text
        assert 'quota_company_monthly_input_tokens_exceeded' in response.text
        assert '成本配額已達上限' in response.text
