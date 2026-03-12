from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
import uuid
from sqlalchemy.orm import Session

from src.core.database import get_db
from src.models import User, Agent, Document
from src.middleware.auth import get_current_user
from src.middleware.rbac import check_permission
from src.api.errors import not_found_error, forbidden_error

router = APIRouter()


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
        filename = uploaded.filename
        content_type = getattr(uploaded, 'content_type', 'application/octet-stream')
    else:
        filename = 'uploaded'
        content_type = 'application/octet-stream'

    doc = Document(
        id=uuid.uuid4(),
        agent_id=agent_id,
        filename=filename,
        file_path=f'/tmp/{filename}',
        file_type=content_type,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    return {
        'document_id': str(doc.id),
        'filename': doc.filename,
        'status': 'processing',
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
                'status': 'ready',
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

    db.delete(doc)
    db.commit()

    return None
