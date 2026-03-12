import pytest
from io import BytesIO


class TestRAGUpload:
    def test_upload_document_admin(self, client, admin_user, admin_token, agent):
        file_content = b'Test document content'
        file = BytesIO(file_content)
        file.name = 'test.txt'

        response = client.post(
            f'/api/rag/upload?agent_id={agent.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            files={'file': ('test.txt', file_content, 'text/plain')},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'document_id' in data
        assert data['filename'] == 'test.txt'

    def test_upload_document_nonexistent_agent(self, client, admin_user, admin_token):
        response = client.post(
            '/api/rag/upload?agent_id=nonexistent-id',
            headers={'Authorization': f'Bearer {admin_token}'},
            files={'file': ('', b'content', 'text/plain')},
        )
        assert response.status_code == 404

    def test_upload_document_regular_user_forbidden(self, client, regular_user, regular_user_token, agent):
        response = client.post(
            f'/api/rag/upload?agent_id={agent.id}',
            headers={'Authorization': f'Bearer {regular_user_token}'},
            files={'file': ('', b'content', 'text/plain')},
        )
        assert response.status_code == 403


class TestRAGDocuments:
    def test_list_documents(self, client, admin_user, admin_token, agent, db):
        from src.models import Document
        import uuid

        doc = Document(
            id=uuid.uuid4(),
            agent_id=agent.id,
            filename='test.txt',
            file_path='/tmp/test.txt',
            file_type='text/plain',
        )
        db.add(doc)
        db.commit()

        response = client.get(
            f'/api/rag/{agent.id}/documents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert 'documents' in data

    def test_list_documents_nonexistent_agent(self, client, admin_user, admin_token):
        response = client.get(
            '/api/rag/nonexistent-id/documents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 404


class TestRAGDocumentDelete:
    def test_delete_document(self, client, admin_user, admin_token, agent, db):
        from src.models import Document
        import uuid

        doc = Document(
            id=uuid.uuid4(),
            agent_id=agent.id,
            filename='test.txt',
            file_path='/tmp/test.txt',
            file_type='text/plain',
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        response = client.delete(
            f'/api/rag/{agent.id}/documents/{doc.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200

    def test_delete_document_nonexistent(self, client, admin_user, admin_token, agent):
        response = client.delete(
            f'/api/rag/{agent.id}/documents/nonexistent-id',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 404
