import pytest


class TestAuthRegister:
    def test_register_success(self, client):
        response = client.post(
            '/api/register',
            json={
                'username': 'newuser',
                'email': 'newuser@test.com',
                'password': 'password123',
                'role': 'user',
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data['username'] == 'newuser'
        assert data['email'] == 'newuser@test.com'
        assert data['role'] == 'user'
        assert 'id' in data

    def test_register_duplicate_email(self, client, admin_user):
        response = client.post(
            '/api/register',
            json={
                'username': 'anotheruser',
                'email': 'admin@test.com',
                'password': 'password123',
                'role': 'user',
            },
        )
        assert response.status_code == 400

    def test_register_duplicate_username(self, client, admin_user):
        response = client.post(
            '/api/register',
            json={
                'username': 'admin',
                'email': 'different@test.com',
                'password': 'password123',
                'role': 'user',
            },
        )
        assert response.status_code == 400

    def test_register_invalid_email(self, client):
        response = client.post(
            '/api/register',
            json={
                'username': 'newuser',
                'email': 'not-an-email',
                'password': 'password123',
                'role': 'user',
            },
        )
        assert response.status_code == 422


class TestAuthLogin:
    def test_login_success(self, client, admin_user):
        response = client.post(
            '/api/login',
            json={
                'email': 'admin@test.com',
                'password': 'admin123',
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert 'token' in data
        assert 'user' in data
        assert data['user']['username'] == 'admin'

    def test_login_wrong_password(self, client, admin_user):
        response = client.post(
            '/api/login',
            json={
                'email': 'admin@test.com',
                'password': 'wrongpassword',
            },
        )
        assert response.status_code == 401

    def test_login_nonexistent_user(self, client):
        response = client.post(
            '/api/login',
            json={
                'email': 'nonexistent@test.com',
                'password': 'password123',
            },
        )
        assert response.status_code == 401

    def test_login_invalid_email(self, client):
        response = client.post(
            '/api/login',
            json={
                'email': 'not-an-email',
                'password': 'password123',
            },
        )
        assert response.status_code == 422

    def test_login_disabled_user(self, client, db):
        from src.models import User
        from src.middleware.auth import get_password_hash

        disabled_user = User(
            username='disabled_user',
            email='disabled_user@test.com',
            password_hash=get_password_hash('disabled123'),
            enabled=False,
        )
        db.add(disabled_user)
        db.commit()

        response = client.post(
            '/api/login',
            json={
                'email': 'disabled_user@test.com',
                'password': 'disabled123',
            },
        )
        assert response.status_code == 401


class TestAuthMe:
    def test_get_current_user(self, client, admin_user, admin_token):
        response = client.get(
            '/api/me',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert data['username'] == 'admin'
        assert data['email'] == 'admin@test.com'

    def test_get_current_user_unauthorized(self, client):
        response = client.get('/api/me')
        assert response.status_code == 403
