from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
import uuid
import os
from pathlib import Path
from datetime import datetime
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import User, Agent, Document, RagDataset
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, forbidden_error
from src.services.qdrant_service import qdrant_service
from src.services.rag_vectorizer import chunk_text
from src.services.embedding_service import embedding_service

router = APIRouter()

UPLOAD_ROOT = Path('/tmp/dyagent_uploads')


def _agent_collection_name(agent_id: str) -> str:
    # 目的：統一代理者私有文件的向量集合命名。
    # 為什麼：讓上傳與檢索流程使用同一來源，避免散落硬編碼。
    return f'agent_{str(agent_id).replace("-", "")}_docs'


def _safe_filename(name: str) -> str:
    # 目的：產生可安全落地的檔名。
    # 為什麼：避免路徑注入與特殊字元造成檔案系統錯誤。
    cleaned = ''.join(ch for ch in (name or '') if ch.isalnum() or ch in {'-', '_', '.', ' '}).strip()
    return cleaned or 'uploaded'


def _global_collection_name(dataset_row: RagDataset) -> str:
    # 目的：統一公有資料集的向量集合名稱。
    # 為什麼：支援 index_name 自訂並提供穩定預設值，避免查詢來源不一致。
    index_name = str(getattr(dataset_row, 'index_name', '') or '').strip()
    if index_name:
        return index_name
    return f"rag_dataset_{str(dataset_row.id).replace('-', '')}"


def _extract_text_for_indexing(content: bytes, content_type: str) -> str:
    # 目的：萃取可索引文字內容。
    # 為什麼：最小可行上傳流程先支援文字型檔案，其他格式先保底索引摘要。
    text_types = {
        'text/plain',
        'text/markdown',
        'application/json',
        'text/csv',
        'application/xml',
        'text/html',
    }
    ct = (content_type or '').lower().split(';')[0].strip()
    if ct in text_types:
        try:
            return content.decode('utf-8')
        except Exception:
            return content.decode('utf-8', errors='ignore')
    return ''


def _index_document_chunks(*, agent_id: str, document_id: str, filename: str, text: str) -> bool:
    # 目的：將文件切塊並寫入向量庫。
    # 為什麼：讓上傳後可立即被 RAG 檢索，縮短配置到可用的路徑。
    chunks = chunk_text(text)
    if not chunks:
        return False

    collection = _agent_collection_name(agent_id)
    qdrant_service.create_collection(collection_name=collection, vector_size=1536)

    vectors = embedding_service.embed_texts(chunks)
    if not vectors:
        return False
    payloads = [
        {
            'document_id': document_id,
            'agent_id': str(agent_id),
            'filename': filename,
            'chunk_index': idx,
            'snippet': chunk[:400],
        }
        for idx, chunk in enumerate(chunks)
    ]
    ids = [f'{document_id}-{idx}' for idx in range(len(chunks))]
    qdrant_service.create_collection(collection_name=collection, vector_size=len(vectors[0]))
    return bool(qdrant_service.upsert_vectors(collection_name=collection, vectors=vectors, payloads=payloads, ids=ids))


def _index_dataset_chunks(*, collection_name: str, dataset_id: str, filename: str, text: str) -> bool:
    # 目的：將公有資料集文件切塊並寫入向量庫。
    # 為什麼：讓公有資料集能由管理頁上傳後立即被綁定代理者使用。
    chunks = chunk_text(text)
    if not chunks:
        return False

    vectors = embedding_service.embed_texts(chunks)
    if not vectors:
        return False

    qdrant_service.create_collection(collection_name=collection_name, vector_size=len(vectors[0]))
    payloads = [
        {
            'dataset_id': dataset_id,
            'filename': filename,
            'chunk_index': idx,
            'snippet': chunk[:400],
            'scope': 'global',
        }
        for idx, chunk in enumerate(chunks)
    ]
    ids = [f'{dataset_id}-{uuid.uuid4()}-{idx}' for idx in range(len(chunks))]
    return bool(qdrant_service.upsert_vectors(collection_name=collection_name, vectors=vectors, payloads=payloads, ids=ids))


def _resolve_document_status(*, current_status: str | None, file_exists: bool) -> str:
    # 目的：回傳可對外展示的文件狀態。
    # 為什麼：歷史資料可能沒有 status 欄位，需有穩健回退避免前端顯示不一致。
    status = str(current_status or '').strip().lower()
    if status in {'uploaded', 'indexing', 'ready', 'failed'}:
        return status
    return 'ready' if file_exists else 'missing'


def _dataset_to_dict(row: RagDataset) -> dict:
    return {
        'id': str(row.id),
        'name': row.name,
        'scope': row.scope,
        'agent_id': str(row.agent_id) if row.agent_id is not None else None,
        'owner_user_id': str(row.owner_user_id) if row.owner_user_id is not None else None,
        'sensitivity': row.sensitivity,
        'vector_backend': row.vector_backend or '',
        'index_name': row.index_name or '',
        'enabled': bool(row.enabled),
        'created_at': row.created_at.isoformat() if row.created_at else None,
        'updated_at': row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post('/rag/upload')
async def upload_document(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        raise forbidden_error()

    # Validate UUID to avoid DB binding errors
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # Manually parse form to handle both UploadFile and empty-filename form field cases
    form = await request.form()
    uploaded = form.get('file')
    if uploaded is not None and hasattr(uploaded, 'filename') and getattr(uploaded, 'filename', None):
        filename = _safe_filename(uploaded.filename)
        content_type = getattr(uploaded, 'content_type', 'application/octet-stream')
        try:
            file_content = await uploaded.read()
        except Exception:
            file_content = b''
    else:
        filename = 'uploaded'
        content_type = 'application/octet-stream'
        file_content = b''

    save_dir = UPLOAD_ROOT / str(agent_id)
    save_dir.mkdir(parents=True, exist_ok=True)
    saved_path = save_dir / f'{uuid.uuid4()}_{filename}'
    with open(saved_path, 'wb') as f:
        f.write(file_content)

    doc = Document(
        id=uuid.uuid4(),
        agent_id=agent_id,
        filename=filename,
        file_path=str(saved_path),
        file_type=content_type,
        status='indexing',
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    extracted_text = _extract_text_for_indexing(file_content, content_type)
    indexed = False
    status = 'uploaded'
    last_error = None
    if not extracted_text.strip():
        status = 'uploaded'
        last_error = 'unsupported_or_empty_content'
    else:
        indexed = _index_document_chunks(
            agent_id=str(agent_id),
            document_id=str(doc.id),
            filename=filename,
            text=extracted_text,
        )
        if indexed:
            status = 'ready'
            last_error = None
        else:
            status = 'failed'
            last_error = 'index_upsert_failed'

    try:
        doc.status = status
        doc.last_error = last_error
        doc.indexed_at = datetime.utcnow() if status == 'ready' else None
        db.commit()
    except Exception:
        db.rollback()

    return {
        'document_id': str(doc.id),
        'filename': doc.filename,
        'status': status,
        'indexed': bool(indexed),
        'last_error': last_error,
    }


@router.get('/rag/{agent_id}/documents')
async def list_documents(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent'):
        raise forbidden_error()

    # Validate UUID to avoid DB binding errors
    try:
        uuid.UUID(str(agent_id))
    except ValueError:
        raise not_found_error('Agent', agent_id)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    documents = db.query(Document).filter(Document.agent_id == agent_id).all()

    return {
        'documents': [
            {
                'id': str(d.id),
                'filename': d.filename,
                'file_type': d.file_type,
                'uploaded_at': d.uploaded_at.isoformat() if d.uploaded_at else None,
                'status': _resolve_document_status(current_status=getattr(d, 'status', None), file_exists=os.path.exists(d.file_path)),
                'last_error': getattr(d, 'last_error', None),
                'indexed_at': d.indexed_at.isoformat() if getattr(d, 'indexed_at', None) else None,
            }
            for d in documents
        ]
    }


@router.delete('/rag/{agent_id}/documents/{doc_id}')
async def delete_document(
    agent_id: str,
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'update_agent'):
        raise forbidden_error()

    # Validate UUIDs
    try:
        uuid.UUID(str(agent_id))
        uuid.UUID(str(doc_id))
    except ValueError:
        raise not_found_error('Document', doc_id)

    doc = db.query(Document).filter(
        Document.id == doc_id,
        Document.agent_id == agent_id,
    ).first()
    if not doc:
        raise not_found_error('Document', doc_id)

    try:
        if doc.file_path and os.path.exists(doc.file_path):
            os.remove(doc.file_path)
    except Exception:
        pass

    db.delete(doc)
    db.commit()

    return None


@router.post('/rag/datasets/{dataset_id}/upload')
async def upload_global_dataset_document(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != 'admin':
        raise forbidden_error()

    row = db.query(RagDataset).filter(RagDataset.id == dataset_id, RagDataset.scope == 'global').first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    form = await request.form()
    uploaded = form.get('file')
    if uploaded is not None and hasattr(uploaded, 'filename') and getattr(uploaded, 'filename', None):
        filename = _safe_filename(uploaded.filename)
        content_type = getattr(uploaded, 'content_type', 'application/octet-stream')
        try:
            file_content = await uploaded.read()
        except Exception:
            file_content = b''
    else:
        filename = 'uploaded'
        content_type = 'application/octet-stream'
        file_content = b''

    save_dir = UPLOAD_ROOT / 'global' / str(dataset_id)
    save_dir.mkdir(parents=True, exist_ok=True)
    saved_path = save_dir / f'{uuid.uuid4()}_{filename}'
    with open(saved_path, 'wb') as f:
        f.write(file_content)

    extracted_text = _extract_text_for_indexing(file_content, content_type)
    collection = _global_collection_name(row)
    indexed = False
    status = 'uploaded'
    if extracted_text.strip():
        indexed = _index_dataset_chunks(
            collection_name=collection,
            dataset_id=str(row.id),
            filename=filename,
            text=extracted_text,
        )
        status = 'ready' if indexed else 'failed'

    return {
        'ok': True,
        'dataset_id': str(row.id),
        'collection': collection,
        'filename': filename,
        'file_path': str(saved_path),
        'status': status,
        'indexed': bool(indexed),
    }


@router.get('/rag/datasets')
async def list_rag_datasets(
    scope: str | None = None,
    agent_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not check_permission(current_user, 'read_agent'):
        raise forbidden_error()

    q = db.query(RagDataset)
    if scope:
        q = q.filter(RagDataset.scope == scope)
    if agent_id:
        q = q.filter(RagDataset.agent_id == agent_id)
    rows = q.order_by(RagDataset.created_at.desc()).all()
    return {'datasets': [_dataset_to_dict(r) for r in rows]}


@router.post('/rag/datasets')
async def create_global_rag_dataset(
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != 'admin':
        raise forbidden_error()

    name = str((payload or {}).get('name') or '').strip()
    if not name:
        from src.api.errors import validation_error
        raise validation_error('name 為必填')
    sensitivity = str((payload or {}).get('sensitivity') or 'normal').strip() or 'normal'
    if sensitivity not in {'normal', 'confidential', 'restricted'}:
        from src.api.errors import validation_error
        raise validation_error('sensitivity 僅允許 normal/confidential/restricted')

    row = RagDataset(
        name=name,
        scope='global',
        agent_id=None,
        owner_user_id=current_user.id,
        sensitivity=sensitivity,
        vector_backend=str((payload or {}).get('vector_backend') or '') or None,
        index_name=str((payload or {}).get('index_name') or '') or None,
        enabled=bool((payload or {}).get('enabled', True)),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {'ok': True, 'dataset': _dataset_to_dict(row)}


@router.put('/rag/datasets/{dataset_id}')
async def update_global_rag_dataset(
    dataset_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != 'admin':
        raise forbidden_error()

    row = db.query(RagDataset).filter(RagDataset.id == dataset_id, RagDataset.scope == 'global').first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    if 'name' in (payload or {}):
        name = str((payload or {}).get('name') or '').strip()
        if not name:
            from src.api.errors import validation_error
            raise validation_error('name 不可為空')
        row.name = name

    if 'sensitivity' in (payload or {}):
        sensitivity = str((payload or {}).get('sensitivity') or '').strip()
        if sensitivity not in {'normal', 'confidential', 'restricted'}:
            from src.api.errors import validation_error
            raise validation_error('sensitivity 僅允許 normal/confidential/restricted')
        row.sensitivity = sensitivity

    if 'vector_backend' in (payload or {}):
        row.vector_backend = str((payload or {}).get('vector_backend') or '').strip() or None
    if 'index_name' in (payload or {}):
        row.index_name = str((payload or {}).get('index_name') or '').strip() or None
    if 'enabled' in (payload or {}):
        row.enabled = bool((payload or {}).get('enabled'))

    db.commit()
    db.refresh(row)
    return {'ok': True, 'dataset': _dataset_to_dict(row)}


@router.delete('/rag/datasets/{dataset_id}')
async def delete_global_rag_dataset(
    dataset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != 'admin':
        raise forbidden_error()

    row = db.query(RagDataset).filter(RagDataset.id == dataset_id, RagDataset.scope == 'global').first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    db.delete(row)
    db.commit()
    return {'ok': True, 'id': dataset_id}
