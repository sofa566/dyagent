from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
import uuid
import os
import io
import csv
import json
import re
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


def _private_collection_name(dataset_row: RagDataset) -> str:
    # 目的：統一私有資料集的向量集合名稱。
    # 為什麼：讓私有資料集與公有資料集使用一致的命名模式，但加上 private 前綴以區分。
    index_name = str(getattr(dataset_row, 'index_name', '') or '').strip()
    if index_name:
        return index_name
    return f"rag_private_{str(dataset_row.id).replace('-', '')}"


def _dataset_collection_name(dataset_row: RagDataset) -> str:
    # 目的：根據資料集 scope 自動選擇正確的 collection 名稱。
    if dataset_row.scope == 'agent_private':
        return _private_collection_name(dataset_row)
    return _global_collection_name(dataset_row)


def _dataset_upload_dir(dataset_row: RagDataset) -> Path:
    # 目的：根據資料集 scope 返回正確的上傳目錄。
    if dataset_row.scope == 'agent_private':
        return UPLOAD_ROOT / 'private' / str(dataset_row.id)
    return UPLOAD_ROOT / 'global' / str(dataset_row.id)


def _extract_text_for_indexing(content: bytes, content_type: str, filename: str) -> tuple[str, str | None]:
    # 目的：萃取可索引文字內容。
    # 為什麼：支援常見文件格式，並在不支援時回傳明確訊息供前端提示。
    text_types = {
        'text/plain',
        'text/markdown',
        'application/json',
        'text/csv',
        'application/xml',
        'text/html',
    }
    ct = (content_type or '').lower().split(';')[0].strip()
    ext = (Path(filename or '').suffix or '').lower()

    def _decode_utf8(raw: bytes) -> str:
        try:
            return raw.decode('utf-8')
        except Exception:
            return raw.decode('utf-8', errors='ignore')

    if ct in text_types:
        if ct == 'text/html' or ext in {'.html', '.htm'}:
            text = _decode_utf8(content)
            text = re.sub(r'<script[\s\S]*?</script>', ' ', text, flags=re.IGNORECASE)
            text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            return re.sub(r'\s+', ' ', text).strip(), None
        if ct == 'application/xml' or ext in {'.xml'}:
            text = _decode_utf8(content)
            text = re.sub(r'<[^>]+>', ' ', text)
            return re.sub(r'\s+', ' ', text).strip(), None
        return _decode_utf8(content), None

    if ext == '.json':
        try:
            data = json.loads(_decode_utf8(content) or '{}')
            return json.dumps(data, ensure_ascii=False, indent=2), None
        except Exception:
            return _decode_utf8(content), None

    if ext == '.csv':
        try:
            decoded = _decode_utf8(content)
            reader = csv.reader(io.StringIO(decoded))
            lines = ['\t'.join(row) for row in reader]
            return '\n'.join(lines), None
        except Exception:
            return _decode_utf8(content), None

    if ext == '.pdf':
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(content))
            pages = []
            for page in reader.pages:
                pages.append((page.extract_text() or '').strip())
            return '\n'.join([p for p in pages if p]), None
        except Exception:
            return '', 'pdf_extraction_failed_or_missing_pypdf'

    if ext == '.docx':
        try:
            from docx import Document as DocxDocument

            doc = DocxDocument(io.BytesIO(content))
            text = '\n'.join([p.text for p in doc.paragraphs if (p.text or '').strip()])
            return text, None
        except Exception:
            return '', 'docx_extraction_failed_or_missing_python_docx'

    if ext in {'.xlsx', '.xlsm'}:
        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            lines: list[str] = []
            for sheet in wb.worksheets:
                lines.append(f'[{sheet.title}]')
                for row in sheet.iter_rows(values_only=True):
                    vals = [str(v) for v in row if v is not None and str(v).strip()]
                    if vals:
                        lines.append('\t'.join(vals))
            return '\n'.join(lines), None
        except Exception:
            return '', 'xlsx_extraction_failed_or_missing_openpyxl'

    if ext in {'.txt', '.md'}:
        return _decode_utf8(content), None

    return '', f'unsupported_file_type:{ext or ct or "unknown"}'


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
    ids = [str(uuid.uuid4()) for _ in range(len(chunks))]
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

    extracted_text, extract_error = _extract_text_for_indexing(file_content, content_type, filename)
    indexed = False
    status = 'uploaded'
    last_error = None
    if not extracted_text.strip():
        status = 'uploaded'
        last_error = extract_error or 'unsupported_or_empty_content'
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
        doc.indexed_at = datetime.now() if status == 'ready' else None
        db.commit()
    except Exception:
        db.rollback()

    return {
        'document_id': str(doc.id),
        'filename': doc.filename,
        'status': status,
        'indexed': bool(indexed),
        'last_error': last_error,
        'message': '文件已索引完成' if status == 'ready' else (f'文件已上傳，但無法索引：{last_error}' if last_error else '文件已上傳'),
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
async def upload_dataset_document(
    dataset_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：上傳文件到資料集（支援 global 和 agent_private）。
    # 為什麼：統一上傳邏輯，讓公有和私有資料集使用同一端點。
    try:
        dataset_uuid = uuid.UUID(str(dataset_id))
    except Exception:
        raise not_found_error('RagDataset', dataset_id)

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid).first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    # 權限檢查：公有資料集需要 admin，私有資料集需要 read_agent 權限
    if row.scope == 'global':
        if current_user.role != 'admin':
            raise forbidden_error()
    elif row.scope == 'agent_private':
        if not check_permission(current_user, 'read_agent'):
            raise forbidden_error()
    else:
        raise forbidden_error()

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

    save_dir = _dataset_upload_dir(row)
    save_dir.mkdir(parents=True, exist_ok=True)
    saved_path = save_dir / f'{uuid.uuid4()}_{filename}'
    with open(saved_path, 'wb') as f:
        f.write(file_content)

    extracted_text, extract_error = _extract_text_for_indexing(file_content, content_type, filename)
    collection = _dataset_collection_name(row)
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
    else:
        status = 'uploaded'

    return {
        'ok': True,
        'dataset_id': str(row.id),
        'collection': collection,
        'filename': filename,
        'file_path': str(saved_path),
        'status': status,
        'indexed': bool(indexed),
        'message': '文件已索引完成' if status == 'ready' else (f'文件已上傳，但無法索引：{extract_error or "unsupported_or_empty_content"}' if not indexed else '文件已上傳'),
        'last_error': (None if indexed else (extract_error or 'unsupported_or_empty_content')),
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


@router.get('/rag/datasets/selectable')
async def list_selectable_rag_datasets(
    agent_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：提供聊天介面可選資料集清單。
    # 為什麼：一般聊天使用者沒有 read_agent 權限，直接呼叫 /rag/datasets 會 403。
    can_chat = check_permission(current_user, 'chat')
    can_read_agent = check_permission(current_user, 'read_agent')
    if not can_chat and not can_read_agent:
        raise forbidden_error()

    if not agent_id:
        if not can_read_agent:
            return {'datasets': []}
        rows = db.query(RagDataset).filter(
            RagDataset.scope == 'global',
            RagDataset.enabled == True,  # noqa: E712
        ).order_by(RagDataset.created_at.desc()).all()
        return {'datasets': [_dataset_to_dict(r) for r in rows]}

    try:
        agent_uuid = uuid.UUID(str(agent_id))
    except Exception:
        raise not_found_error('Agent', agent_id) from None

    agent = db.query(Agent).filter(Agent.id == agent_uuid, Agent.enabled == True).first()  # noqa: E712
    if agent is None:
        raise not_found_error('Agent', agent_id)

    rag_cfg = agent.rag_config if isinstance(agent.rag_config, dict) else {}
    global_ids = [str(x) for x in list((rag_cfg or {}).get('global_dataset_ids') or []) if x]
    private_ids = [str(x) for x in list((rag_cfg or {}).get('private_dataset_ids') or []) if x]
    dataset_ids = list(dict.fromkeys(global_ids + private_ids))
    if not dataset_ids:
        return {'datasets': []}

    rows = db.query(RagDataset).filter(
        RagDataset.id.in_(dataset_ids),
        RagDataset.enabled == True,  # noqa: E712
    ).all()

    out = []
    for row in rows:
        if row.scope == 'global':
            out.append(row)
            continue
        if row.scope == 'agent_private' and str(getattr(row, 'agent_id', '') or '') == str(agent.id):
            out.append(row)

    out = sorted(out, key=lambda x: x.created_at or datetime.min, reverse=True)
    return {'datasets': [_dataset_to_dict(r) for r in out]}


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

    try:
        dataset_uuid = uuid.UUID(str(dataset_id))
    except Exception:
        raise not_found_error('RagDataset', dataset_id)

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid, RagDataset.scope == 'global').first()
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

    try:
        dataset_uuid = uuid.UUID(str(dataset_id))
    except Exception:
        raise not_found_error('RagDataset', dataset_id)

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid, RagDataset.scope == 'global').first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    db.delete(row)
    db.commit()
    return {'ok': True, 'id': dataset_id}


@router.get('/rag/datasets/{dataset_id}/documents')
async def list_dataset_documents(
    dataset_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：列出資料集已上傳的文件清單（支援 global 和 agent_private）。
    # 為什麼：讓前端顯示已上傳檔案避免重複上傳，並提供文件數量統計。
    try:
        dataset_uuid = uuid.UUID(str(dataset_id))
    except Exception:
        raise not_found_error('RagDataset', dataset_id) from None

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid).first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    # 權限檢查：公有資料集需要 admin，私有資料集需要 read_agent 權限
    if row.scope == 'global':
        if current_user.role != 'admin':
            raise forbidden_error()
    elif row.scope == 'agent_private':
        if not check_permission(current_user, 'read_agent'):
            raise forbidden_error()
    else:
        raise forbidden_error()

    upload_dir = _dataset_upload_dir(row)
    documents = []
    if upload_dir.is_dir():
        for fname in os.listdir(upload_dir):
            fpath = upload_dir / fname
            if fpath.is_file():
                stat = fpath.stat()
                # 去掉 UUID 前綴顯示原始檔名（格式：{uuid}_{原始檔名}）
                display_name = fname.split('_', 1)[1] if '_' in fname else fname
                documents.append({
                    'filename': display_name,
                    'file_path': str(fpath),
                    'size_bytes': stat.st_size,
                    'uploaded_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })

    # 依上傳時間倒序
    documents.sort(key=lambda d: d['uploaded_at'], reverse=True)

    return {
        'ok': True,
        'dataset_id': str(row.id),
        'documents': documents,
        'total_count': len(documents),
    }


@router.post('/rag/datasets/{dataset_id}/search')
async def search_dataset(
    dataset_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：對資料集執行向量檢索測試（支援 global 和 agent_private）。
    # 為什麼：讓管理員/代理者管理員可在上傳文件後驗證索引是否正常運作。
    import time
    start = time.time()

    query = str((payload or {}).get('query') or '').strip()
    limit = int((payload or {}).get('limit') or 5)
    if not query:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail='query_required')

    try:
        dataset_uuid = uuid.UUID(str(dataset_id))
    except Exception:
        raise not_found_error('RagDataset', dataset_id) from None

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid).first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    # 權限檢查：公有資料集需要 admin，私有資料集需要 read_agent 權限
    if row.scope == 'global':
        if current_user.role != 'admin':
            raise forbidden_error()
    elif row.scope == 'agent_private':
        if not check_permission(current_user, 'read_agent'):
            raise forbidden_error()
    else:
        raise forbidden_error()

    collection = _dataset_collection_name(row)

    try:
        query_vector = embedding_service.embed_one(query)
        results = qdrant_service.search(collection, query_vector, limit=limit)
        elapsed_ms = int((time.time() - start) * 1000)

        return {
            'ok': True,
            'dataset_id': str(row.id),
            'collection': collection,
            'query': query,
            'elapsed_ms': elapsed_ms,
            'results': results,
            'total_found': len(results),
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return {
            'ok': False,
            'error': str(e),
            'message': '查詢失敗',
            'elapsed_ms': elapsed_ms,
        }
