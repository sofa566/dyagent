import pytest
from src.models import Log


class TestLogsList:
    def test_get_logs_admin(self, client, admin_user, admin_token, db):
        log = Log(
            user_id=admin_user.id,
            level='info',
            action='test_action',
            details={'key': 'value'},
        )
        db.add(log)
        db.commit()

        response = client.get(
            '/api/logs',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'logs' in data
        assert data['total'] >= 1

    def test_get_logs_with_level_filter(self, client, admin_user, admin_token, db):
        log = Log(
            user_id=admin_user.id,
            level='error',
            action='test_action',
        )
        db.add(log)
        db.commit()

        response = client.get(
            '/api/logs?level=error',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert all(l['level'] == 'error' for l in data['logs'])

    def test_get_logs_pagination(self, client, admin_user, admin_token, db):
        for i in range(5):
            log = Log(
                user_id=admin_user.id,
                level='info',
                action=f'action_{i}',
            )
            db.add(log)
        db.commit()

        response = client.get(
            '/api/logs?page=1&limit=2',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data['logs']) == 2
        assert data['total'] == 5

    def test_get_logs_regular_user_forbidden(self, client, regular_user, regular_user_token):
        response = client.get(
            '/api/logs',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )
        assert response.status_code == 403


class TestLogGet:
    def test_get_log_by_id(self, client, admin_user, admin_token, db):
        log = Log(
            user_id=admin_user.id,
            level='info',
            action='test_action',
        )
        db.add(log)
        db.commit()
        db.refresh(log)

        response = client.get(
            f'/api/logs/{log.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert data['action'] == 'test_action'

    def test_get_log_nonexistent(self, client, admin_user, admin_token):
        response = client.get(
            '/api/logs/nonexistent-id',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 404
