import json
import uuid
from datetime import datetime, timedelta
from io import BytesIO

from src.api.routes import rag as rag_routes
from src.models import AccessGroup, AccessPermission, Document, GroupPermissionBinding, RagDataset


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

    def test_upload_document_large_file_marks_uploaded_with_explicit_error(
        self,
        client,
        admin_token,
        agent,
        monkeypatch,
    ):
        # 目的：驗證超過同步索引上限時，仍可上傳成功並回傳可診斷錯誤。
        # 為什麼：避免大檔上傳出現模糊失敗訊息，讓前端可提示後續流程。
        monkeypatch.setattr(rag_routes, 'MAX_SYNC_INDEX_FILE_BYTES', 4)

        response = client.post(
            f'/api/rag/upload?agent_id={agent.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
            files={'file': ('large.txt', b'12345', 'text/plain')},
        )

        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'indexing'
        assert data['indexed'] is False
        assert data['size_bytes'] == 5
        assert data['last_error'].startswith('file_too_large_for_sync_index:')
        assert '背景索引佇列' in data['message']

    def test_upload_dataset_document_large_file_returns_size_and_error(
        self,
        client,
        db,
        admin_token,
        admin_user,
        monkeypatch,
    ):
        # 目的：驗證資料集上傳超過同步索引上限時，回傳狀態與大小資訊。
        # 為什麼：資料集頁面需依據錯誤碼與檔案大小顯示可操作提示。
        dataset_row = RagDataset(name='large-file-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        monkeypatch.setattr(rag_routes, 'MAX_SYNC_INDEX_FILE_BYTES', 4)

        response = client.post(
            f'/api/rag/datasets/{dataset_row.id}/upload',
            headers={'Authorization': f'Bearer {admin_token}'},
            files={'file': ('large.txt', b'12345', 'text/plain')},
        )

        assert response.status_code == 200
        data = response.json()
        assert data['ok'] is True
        assert data['status'] == 'indexing'
        assert data['indexed'] is False
        assert data['size_bytes'] == 5
        assert data['last_error'].startswith('file_too_large_for_sync_index:')
        assert '背景索引佇列' in data['message']


class TestRAGDocuments:
    def test_list_documents(self, client, admin_user, admin_token, agent, db):
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


class TestPdfExtractionFallback:
    def test_extract_text_for_indexing_pdf_fallback_to_docling(self, monkeypatch):
        # 目的：驗證 pypdf 抽取為空時，會自動走 docling 備援抽取。
        # 為什麼：避免可讀 PDF 因單一抽取器限制而誤判無法索引。
        monkeypatch.setattr(
            rag_routes,
            '_extract_pdf_text_with_pypdf',
            lambda _content: ('', 'pdf_empty_text_from_pypdf'),
        )
        monkeypatch.setattr(
            rag_routes,
            '_extract_pdf_text_with_docling',
            lambda _path: ('docling parsed text', None),
        )

        extracted_text, extract_error = rag_routes._extract_text_for_indexing(
            content=b'%PDF-1.4 test',
            content_type='application/pdf',
            filename='sample.pdf',
            file_path='/tmp/sample.pdf',
        )

        assert extracted_text == 'docling parsed text'
        assert extract_error is None

    def test_extract_text_for_indexing_pdf_combines_extractor_errors(self, monkeypatch):
        # 目的：驗證 PDF 兩層抽取都失敗時，會回傳可定位原因的錯誤碼。
        # 為什麼：提升上傳失敗診斷可觀測性，避免全部混成同一訊息。
        monkeypatch.setattr(
            rag_routes,
            '_extract_pdf_text_with_pypdf',
            lambda _content: ('', 'pdf_extraction_failed_or_missing_pypdf:ImportError'),
        )
        monkeypatch.setattr(
            rag_routes,
            '_extract_pdf_text_with_docling',
            lambda _path: ('', 'pdf_docling_fallback_failed:docling_import_failed:ModuleNotFoundError'),
        )

        extracted_text, extract_error = rag_routes._extract_text_for_indexing(
            content=b'%PDF-1.4 test',
            content_type='application/pdf',
            filename='sample.pdf',
            file_path='/tmp/sample.pdf',
        )

        assert extracted_text == ''
        assert extract_error is not None
        assert extract_error.startswith('pdf_all_extractors_failed:')


class TestRagPageNumbering:
    def test_build_chunks_for_indexing_pdf_keeps_page_number(self, monkeypatch):
        # 目的：驗證 PDF 切塊會保留頁碼。
        # 為什麼：檢索結果需回傳來源頁碼，若索引時遺失則前端無法顯示。
        monkeypatch.setattr(
            rag_routes,
            '_extract_pdf_page_texts_with_pypdf',
            lambda _content: (['第一頁內容', '第二頁內容'], None),
        )

        chunks = rag_routes._build_chunks_for_indexing(
            text='第一頁內容\n\n第二頁內容',
            filename='sample.pdf',
            content=b'%PDF-1.4 test',
        )

        assert len(chunks) >= 2
        assert chunks[0]['page_number'] == 1
        assert chunks[1]['page_number'] == 2

    def test_index_dataset_chunks_payload_contains_page_number(self, monkeypatch):
        # 目的：驗證資料集索引 payload 會攜帶頁碼。
        # 為什麼：後續 search API 直接回傳 payload，需確保頁碼可被查詢端取得。
        captured_payloads = []

        monkeypatch.setattr(rag_routes.qdrant_service, 'create_collection', lambda **_kwargs: True)
        monkeypatch.setattr(rag_routes.embedding_service, 'embed_texts', lambda texts: [[0.1, 0.2] for _ in texts])

        def _fake_upsert(**kwargs):
            captured_payloads.extend(kwargs.get('payloads') or [])
            return True

        monkeypatch.setattr(rag_routes.qdrant_service, 'upsert_vectors', _fake_upsert)

        ok, index_error = rag_routes._index_dataset_chunks(
            collection_name='dataset_test',
            dataset_id='dataset-1',
            filename='sample.pdf',
            file_key='upload_key_sample.pdf',
            chunks_with_meta=[
                {'text': '第一頁片段', 'page_number': 1},
                {'text': '第二頁片段', 'page_number': 2},
            ],
        )

        assert ok is True
        assert index_error is None
        assert len(captured_payloads) == 2
        assert captured_payloads[0]['page_number'] == 1
        assert captured_payloads[1]['page_number'] == 2
        assert captured_payloads[0]['file_key'] == 'upload_key_sample.pdf'


class TestRagDatasetDocumentDelete:
    def test_list_dataset_documents_contains_file_key(self, client, db, admin_token, admin_user):
        dataset_row = RagDataset(name='delete-test-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_sample.txt'
        (upload_dir / file_key).write_text('hello world', encoding='utf-8')

        response = client.get(
            f'/api/rag/datasets/{dataset_row.id}/documents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert data['ok'] is True
        assert len(data['documents']) == 1
        assert data['documents'][0]['file_key'] == file_key

    def test_open_dataset_document_allows_chat_user_for_global_normal(self, client, db, admin_user, regular_user_token):
        # 目的：驗證聊天使用者可直接開啟 global normal 資料集來源檔。
        # 為什麼：聊天引用來源需可點擊另開瀏覽器，否則無法完成來源驗證閉環。
        dataset_row = RagDataset(name='open-doc-global', scope='global', enabled=True, sensitivity='normal', owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_sample-open.txt'
        target_file_path = upload_dir / file_key
        target_file_path.write_text('openable content', encoding='utf-8')

        response = client.get(
            f'/api/rag/datasets/{dataset_row.id}/documents/{file_key}/open',
            headers={'Authorization': f'Bearer {regular_user_token}'},
        )

        assert response.status_code == 200
        assert response.text == 'openable content'

    def test_open_dataset_document_allows_anonymous_for_global_normal(self, client, db, admin_user):
        # 目的：驗證 global normal 資料集文件可由匿名請求直接開啟。
        # 為什麼：聊天引用連結可能被使用者直接貼到瀏覽器，需可無 token 打開來源。
        dataset_row = RagDataset(name='open-doc-anon-global', scope='global', enabled=True, sensitivity='normal', owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_sample-anon.txt'
        target_file_path = upload_dir / file_key
        target_file_path.write_text('anonymous open content', encoding='utf-8')

        response = client.get(f'/api/rag/datasets/{dataset_row.id}/documents/{file_key}/open')

        assert response.status_code == 200
        assert response.text == 'anonymous open content'


class TestRagDatasetPermissionCleanup:
    def test_delete_global_dataset_removes_entity_execute_permission(self, client, db, admin_token, admin_user):
        dataset_row = RagDataset(name='cleanup-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.flush()

        permission_key = f'entity.dataset.{dataset_row.id}.execute'
        permission_row = AccessPermission(key=permission_key)
        group_row = AccessGroup(code='cleanup_global_group', name='Cleanup Global Group', enabled=True)
        db.add_all([permission_row, group_row])
        db.flush()
        permission_id = permission_row.id
        db.add(GroupPermissionBinding(group_id=group_row.id, permission_id=permission_row.id))
        db.commit()

        response = client.delete(
            f'/api/rag/datasets/{dataset_row.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        assert db.query(AccessPermission).filter(AccessPermission.key == permission_key).first() is None
        assert db.query(GroupPermissionBinding).filter(GroupPermissionBinding.permission_id == permission_id).count() == 0

    def test_delete_agent_private_dataset_removes_entity_execute_permission(self, client, db, admin_token, admin_user, agent):
        dataset_row = RagDataset(
            name='cleanup-private',
            scope='agent_private',
            agent_id=agent.id,
            enabled=True,
            owner_user_id=admin_user.id,
        )
        db.add(dataset_row)
        db.flush()

        permission_key = f'entity.dataset.{dataset_row.id}.execute'
        permission_row = AccessPermission(key=permission_key)
        group_row = AccessGroup(code='cleanup_private_group', name='Cleanup Private Group', enabled=True)
        db.add_all([permission_row, group_row])
        db.flush()
        permission_id = permission_row.id
        db.add(GroupPermissionBinding(group_id=group_row.id, permission_id=permission_row.id))
        db.commit()

        response = client.delete(
            f'/api/agents/{agent.id}/rag/datasets/{dataset_row.id}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        assert db.query(AccessPermission).filter(AccessPermission.key == permission_key).first() is None
        assert db.query(GroupPermissionBinding).filter(GroupPermissionBinding.permission_id == permission_id).count() == 0

    def test_open_dataset_document_preview_renders_docx_as_html(self, client, db, admin_user, monkeypatch):
        # 目的：驗證 docx 可透過 open-preview 直接瀏覽，不只下載。
        # 為什麼：瀏覽器通常不支援 docx inline，需轉成 HTML 供使用者快速驗證引用。
        dataset_row = RagDataset(name='open-preview-docx', scope='global', enabled=True, sensitivity='normal', owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_sample-preview.docx'
        target_file_path = upload_dir / file_key
        target_file_path.write_bytes(b'PK\x03\x04docx-placeholder')

        monkeypatch.setattr(rag_routes, 'convert_document_to_markdown', lambda _path: ('# 標題\n- 重點一', None))

        response = client.get(f'/api/rag/datasets/{dataset_row.id}/documents/{file_key}/open-preview')

        assert response.status_code == 200
        assert 'text/html' in str(response.headers.get('content-type') or '')
        assert '以下內容由文件轉換為可閱讀預覽格式' in response.text
        assert '重點一' in response.text

    def test_delete_dataset_document_removes_file_and_vectors(self, client, db, admin_token, admin_user, monkeypatch):
        dataset_row = RagDataset(name='delete-test-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_sample.txt'
        target_file_path = upload_dir / file_key
        target_file_path.write_text('hello world', encoding='utf-8')

        called_fields: list[dict] = []

        def _fake_delete_by_payload_match(*, collection_name, match_fields):
            called_fields.append({'collection_name': collection_name, 'match_fields': match_fields})
            return True

        monkeypatch.setattr(rag_routes.qdrant_service, 'delete_by_payload_match', _fake_delete_by_payload_match)

        response = client.delete(
            f'/api/rag/datasets/{dataset_row.id}/documents/{file_key}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )
        assert response.status_code == 200
        data = response.json()
        assert data['ok'] is True
        assert data['file_deleted'] is True
        assert target_file_path.exists() is False

        assert len(called_fields) >= 1
        assert called_fields[0]['match_fields']['dataset_id'] == str(dataset_row.id)
        assert called_fields[0]['match_fields']['file_key'] == file_key

    def test_delete_dataset_document_rejects_when_indexing(self, client, db, admin_token, admin_user):
        # 目的：驗證索引進行中不可刪除文件。
        # 為什麼：避免刪除與背景索引併發導致狀態競態與資料不一致。
        dataset_row = RagDataset(name='delete-test-indexing', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_sample-indexing.txt'
        target_file_path = upload_dir / file_key
        target_file_path.write_text('indexing file', encoding='utf-8')

        progress_file_path = rag_routes._dataset_progress_file_path(dataset_row=dataset_row, file_key=file_key)
        rag_routes._write_progress_file(
            progress_file_path=str(progress_file_path),
            payload={
                'status': 'indexing',
                'stage': 'embedding_upsert',
                'progress': 42,
                'file_key': file_key,
                'filename': 'sample-indexing.txt',
            },
        )

        response = client.delete(
            f'/api/rag/datasets/{dataset_row.id}/documents/{file_key}',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 400
        error_detail = response.json().get('detail') or {}
        assert '排隊或索引中' in str(error_detail.get('error') or '')
        assert target_file_path.exists() is True


class TestRagBackgroundProgressFailure:
    def test_dataset_background_index_extract_exception_marks_progress_failed(self, monkeypatch, tmp_path):
        # 目的：驗證背景抽取拋錯時，進度檔會改為 failed，而非卡在 extracting。
        # 為什麼：多檔背景索引若其中一檔失敗，前端需要看到失敗而不是永遠停在低進度。
        progress_file_path = tmp_path / 'progress' / 'file_a.json'

        monkeypatch.setattr(rag_routes, '_ensure_embedding_ready_for_indexing', lambda: (True, None))
        monkeypatch.setattr(rag_routes, '_ensure_qdrant_ready_for_indexing', lambda: (True, None))

        def _raise_extract_error(**_kwargs):
            raise RuntimeError('extract boom')

        monkeypatch.setattr(rag_routes, '_extract_text_for_indexing_from_path', _raise_extract_error)

        rag_routes._run_dataset_document_background_index(
            collection_name='rag_dataset_x',
            dataset_id='dataset-x',
            filename='sample.txt',
            file_key='file-a',
            file_path='/tmp/not-exists.txt',
            content_type='text/plain',
            progress_file_path=str(progress_file_path),
        )

        payload = rag_routes._read_progress_file(progress_file_path=str(progress_file_path))
        assert payload.get('status') == 'failed'
        assert payload.get('stage') == 'failed'
        assert payload.get('progress') == 100
        assert str(payload.get('last_error') or '').startswith('background_index_file_read_failed:')

    def test_dataset_index_process_entry_exception_marks_progress_failed(self, monkeypatch, tmp_path):
        # 目的：驗證子進程入口未捕獲例外時，仍會回寫 failed 進度。
        # 為什麼：若子進程崩潰而不回寫狀態，前端會誤判卡住。
        progress_file_path = tmp_path / 'progress' / 'file_b.json'

        def _raise_process_error(**_kwargs):
            raise ValueError('process crash')

        monkeypatch.setattr(rag_routes, '_run_dataset_document_background_index', _raise_process_error)

        rag_routes._run_dataset_document_index_process_entry(
            collection_name='rag_dataset_x',
            dataset_id='dataset-x',
            filename='sample-b.pdf',
            file_key='file-b',
            file_path='/tmp/not-exists-b.pdf',
            content_type='application/pdf',
            progress_file_path=str(progress_file_path),
        )

        payload = rag_routes._read_progress_file(progress_file_path=str(progress_file_path))
        assert payload.get('status') == 'failed'
        assert payload.get('stage') == 'failed'
        assert payload.get('progress') == 100
        assert str(payload.get('last_error') or '').startswith('index_process_failed:')

    def test_write_progress_file_keeps_existing_process_pid(self, tmp_path):
        # 目的：驗證多階段回寫進度時會保留 process_pid。
        # 為什麼：列表端需依 PID 判斷子進程是否終止，若被覆蓋會造成誤判。
        progress_file_path = tmp_path / 'progress' / 'file_c.json'
        rag_routes._write_progress_file(
            progress_file_path=str(progress_file_path),
            payload={
                'status': 'indexing',
                'stage': 'starting',
                'progress': 1,
                'process_pid': 99999,
            },
        )

        rag_routes._write_progress_file(
            progress_file_path=str(progress_file_path),
            payload={
                'status': 'indexing',
                'stage': 'extracting',
                'progress': 5,
            },
        )

        payload = rag_routes._read_progress_file(progress_file_path=str(progress_file_path))
        assert payload.get('stage') == 'extracting'
        assert int(payload.get('process_pid') or 0) == 99999

    def test_dataset_background_index_pdf_no_valid_chunks_uses_docling_fallback(self, monkeypatch, tmp_path):
        # 目的：驗證 PDF 走 pypdf 無 chunk 時，會改用 docling 備援再嘗試索引。
        # 為什麼：大型掃描 PDF 常見 pypdf 抽不到文字，若不備援會直接 failed(no_valid_chunks)。
        progress_file_path = tmp_path / 'progress' / 'file_pdf.json'
        input_file_path = tmp_path / 'sample.pdf'
        input_file_path.write_bytes(b'%PDF-1.4')

        monkeypatch.setattr(rag_routes, '_ensure_embedding_ready_for_indexing', lambda: (True, None))
        monkeypatch.setattr(rag_routes, '_ensure_qdrant_ready_for_indexing', lambda: (True, None))

        index_calls = {'count': 0}

        def _fake_index_dataset_chunks(**_kwargs):
            index_calls['count'] += 1
            if index_calls['count'] == 1:
                return False, 'no_valid_chunks'
            return True, None

        monkeypatch.setattr(rag_routes, '_index_dataset_chunks', _fake_index_dataset_chunks)
        monkeypatch.setattr(rag_routes, '_extract_pdf_text_with_docling', lambda _p: ('docling fallback text', None))
        monkeypatch.setattr(rag_routes, '_acquire_pdf_docling_lock', lambda: object())
        monkeypatch.setattr(rag_routes, '_release_pdf_docling_lock', lambda _lock: None)

        rag_routes._run_dataset_document_background_index(
            collection_name='rag_dataset_x',
            dataset_id='dataset-x',
            filename='sample.pdf',
            file_key='file-pdf',
            file_path=str(input_file_path),
            content_type='application/pdf',
            progress_file_path=str(progress_file_path),
        )

        payload = rag_routes._read_progress_file(progress_file_path=str(progress_file_path))
        assert index_calls['count'] == 2
        assert payload.get('status') == 'ready'
        assert payload.get('stage') == 'done'
        assert payload.get('progress') == 100

    def test_list_dataset_documents_keeps_stale_extracting_status_when_pid_unknown(self, client, db, admin_token, admin_user):
        # 目的：驗證 extracting 階段超時但缺少 PID 時，不會直接標記為 failed。
        # 為什麼：大型 PDF 抽取可能超過 timeout，僅靠時間會誤判為背景流程中止。
        dataset_row = RagDataset(name='stale-extracting-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_stale-extracting.txt'
        (upload_dir / file_key).write_text('stale extracting file', encoding='utf-8')

        stale_updated_at = (datetime.now() - timedelta(minutes=20)).isoformat()
        progress_file_path = rag_routes._dataset_progress_file_path(dataset_row=dataset_row, file_key=file_key)
        progress_file_path.parent.mkdir(parents=True, exist_ok=True)
        progress_file_path.write_text(
            json.dumps({
                'status': 'indexing',
                'stage': 'extracting',
                'progress': 5,
                'updated_at': stale_updated_at,
            }, ensure_ascii=False),
            encoding='utf-8',
        )

        response = client.get(
            f'/api/rag/datasets/{dataset_row.id}/documents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get('ok') is True
        assert len(data.get('documents') or []) == 1
        document_row = data['documents'][0]
        assert document_row['status'] == 'indexing'
        assert int(document_row['progress'] or 0) == 5
        assert document_row['last_error'] is None

    def test_list_dataset_documents_marks_stale_indexing_with_dead_pid_as_failed(self, client, db, admin_token, admin_user, monkeypatch):
        # 目的：驗證 indexing 超時且 PID 已不存在時，會標記為 failed。
        # 為什麼：子進程若被系統終止，需立即回寫可診斷狀態，避免前端卡住。
        dataset_row = RagDataset(name='stale-dead-pid-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_stale-dead-pid.txt'
        (upload_dir / file_key).write_text('stale dead pid file', encoding='utf-8')

        stale_updated_at = (datetime.now() - timedelta(minutes=20)).isoformat()
        progress_file_path = rag_routes._dataset_progress_file_path(dataset_row=dataset_row, file_key=file_key)
        progress_file_path.parent.mkdir(parents=True, exist_ok=True)
        progress_file_path.write_text(
            json.dumps({
                'status': 'indexing',
                'stage': 'embedding_upsert',
                'progress': 77,
                'process_pid': 456789,
                'updated_at': stale_updated_at,
            }, ensure_ascii=False),
            encoding='utf-8',
        )

        monkeypatch.setattr(rag_routes, '_is_process_alive', lambda _pid: False)

        response = client.get(
            f'/api/rag/datasets/{dataset_row.id}/documents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get('ok') is True
        assert len(data.get('documents') or []) == 1
        document_row = data['documents'][0]
        assert document_row['status'] == 'failed'
        assert int(document_row['progress'] or 0) == 100
        assert document_row['last_error'] == 'index_process_terminated:pid=456789'

    def test_list_dataset_documents_marks_stale_docling_extracting_as_failed_when_pid_unknown(self, client, db, admin_token, admin_user):
        # 目的：驗證 docling_extracting 階段停滯且沒有 PID 時，會標記為 failed。
        # 為什麼：docling 階段卡住時若沒有 pid 可檢查，前端會長時間停在 33%。
        dataset_row = RagDataset(name='stale-docling-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes.UPLOAD_ROOT / 'global' / str(dataset_row.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_stale-docling.txt'
        (upload_dir / file_key).write_text('stale docling file', encoding='utf-8')

        stale_updated_at = (datetime.now() - timedelta(minutes=20)).isoformat()
        progress_file_path = rag_routes._dataset_progress_file_path(dataset_row=dataset_row, file_key=file_key)
        progress_file_path.parent.mkdir(parents=True, exist_ok=True)
        progress_file_path.write_text(
            json.dumps({
                'status': 'indexing',
                'stage': 'docling_extracting',
                'progress': 33,
                'updated_at': stale_updated_at,
            }, ensure_ascii=False),
            encoding='utf-8',
        )

        response = client.get(
            f'/api/rag/datasets/{dataset_row.id}/documents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get('ok') is True
        assert len(data.get('documents') or []) == 1
        document_row = data['documents'][0]
        assert document_row['status'] == 'failed'
        assert int(document_row['progress'] or 0) == 100
        assert document_row['last_error'] == 'docling_extracting_stalled_or_process_terminated'


class TestRagIndexQueueDispatch:
    def test_dispatch_respects_max_concurrency(self, monkeypatch, tmp_path):
        # 目的：驗證派工會遵守並行上限，不會在滿載時再啟動子進程。
        # 為什麼：大量檔案同時上傳時，需以佇列緩衝防止記憶體爆量。
        monkeypatch.setattr(rag_routes, 'UPLOAD_ROOT', tmp_path)
        monkeypatch.setattr(rag_routes.settings, 'RAG_INDEX_MAX_CONCURRENCY', 2)

        progress_dir = tmp_path / 'global' / 'dataset-a' / rag_routes.INDEX_PROGRESS_DIR_NAME
        progress_dir.mkdir(parents=True, exist_ok=True)
        payload = {'status': 'indexing', 'updated_at': datetime.now().isoformat(), 'progress': 10}
        (progress_dir / 'active-1.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        (progress_dir / 'active-2.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

        rag_routes._enqueue_index_job(
            job_type='dataset_document',
            kwargs={
                'collection_name': 'rag_dataset_a',
                'dataset_id': 'dataset-a',
                'filename': 'queued-a.pdf',
                'file_key': 'queued-a',
                'file_path': '/tmp/queued-a.pdf',
                'content_type': 'application/pdf',
                'progress_file_path': str(progress_dir / 'queued-a.json'),
            },
        )

        spawn_calls = []

        def _fake_spawn_index_process(**kwargs):
            spawn_calls.append(kwargs)
            return 12345

        monkeypatch.setattr(rag_routes, '_spawn_index_process', _fake_spawn_index_process)

        started = rag_routes._dispatch_index_jobs_once()

        assert started == 0
        assert len(spawn_calls) == 0
        queue_files = sorted(rag_routes._index_queue_dir().glob('*.json'))
        assert len(queue_files) == 1

    def test_dispatch_starts_job_when_slot_available(self, monkeypatch, tmp_path):
        # 目的：驗證有空閒 slot 時，佇列任務會被啟動並更新進度。
        # 為什麼：確保派工機制能自動消化 queued 任務。
        monkeypatch.setattr(rag_routes, 'UPLOAD_ROOT', tmp_path)
        monkeypatch.setattr(rag_routes.settings, 'RAG_INDEX_MAX_CONCURRENCY', 2)

        progress_dir = tmp_path / 'global' / 'dataset-b' / rag_routes.INDEX_PROGRESS_DIR_NAME
        progress_dir.mkdir(parents=True, exist_ok=True)
        active_payload = {'status': 'indexing', 'updated_at': datetime.now().isoformat(), 'progress': 30}
        (progress_dir / 'active-1.json').write_text(json.dumps(active_payload, ensure_ascii=False), encoding='utf-8')

        queued_progress_path = progress_dir / 'queued-b.json'
        rag_routes._enqueue_index_job(
            job_type='dataset_document',
            kwargs={
                'collection_name': 'rag_dataset_b',
                'dataset_id': 'dataset-b',
                'filename': 'queued-b.pdf',
                'file_key': 'queued-b',
                'file_path': '/tmp/queued-b.pdf',
                'content_type': 'application/pdf',
                'progress_file_path': str(queued_progress_path),
            },
        )

        monkeypatch.setattr(rag_routes, '_spawn_index_process', lambda **_kwargs: 54321)

        started = rag_routes._dispatch_index_jobs_once()

        assert started == 1
        queue_files = sorted(rag_routes._index_queue_dir().glob('*.json'))
        assert len(queue_files) == 0
        queued_payload = rag_routes._read_progress_file(progress_file_path=str(queued_progress_path))
        assert queued_payload.get('status') == 'indexing'
        assert queued_payload.get('stage') == 'starting'
        assert int(queued_payload.get('process_pid') or 0) == 54321

    def test_list_dataset_documents_triggers_dispatch(self, client, db, admin_token, admin_user, monkeypatch, tmp_path):
        # 目的：驗證前端輪詢 documents 列表時，API 進程會觸發 queued 任務派工。
        # 為什麼：避免依賴子進程鏈式再派工，造成進程堆疊與記憶體壓力。
        monkeypatch.setattr(rag_routes, 'UPLOAD_ROOT', tmp_path)
        monkeypatch.setattr(rag_routes.settings, 'RAG_INDEX_MAX_CONCURRENCY', 1)

        dataset_row = RagDataset(name='dispatch-by-list-global', scope='global', enabled=True, owner_user_id=admin_user.id)
        db.add(dataset_row)
        db.commit()
        db.refresh(dataset_row)

        upload_dir = rag_routes._dataset_upload_dir(dataset_row)
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_key = f'{uuid.uuid4()}_queued.txt'
        (upload_dir / file_key).write_text('queued', encoding='utf-8')

        progress_file_path = rag_routes._dataset_progress_file_path(dataset_row=dataset_row, file_key=file_key)
        rag_routes._write_progress_file(
            progress_file_path=str(progress_file_path),
            payload={
                'status': 'queued',
                'stage': 'queued',
                'progress': 0,
                'file_key': file_key,
                'filename': 'queued.txt',
            },
        )

        rag_routes._enqueue_index_job(
            job_type='dataset_document',
            kwargs={
                'collection_name': rag_routes._dataset_collection_name(dataset_row),
                'dataset_id': str(dataset_row.id),
                'filename': 'queued.txt',
                'file_key': file_key,
                'file_path': str(upload_dir / file_key),
                'content_type': 'text/plain',
                'progress_file_path': str(progress_file_path),
            },
        )

        monkeypatch.setattr(rag_routes, '_spawn_index_process', lambda **_kwargs: 23456)

        response = client.get(
            f'/api/rag/datasets/{dataset_row.id}/documents',
            headers={'Authorization': f'Bearer {admin_token}'},
        )

        assert response.status_code == 200
        queued_payload = rag_routes._read_progress_file(progress_file_path=str(progress_file_path))
        assert queued_payload.get('status') == 'indexing'
        assert queued_payload.get('stage') == 'starting'
        assert int(queued_payload.get('process_pid') or 0) == 23456
