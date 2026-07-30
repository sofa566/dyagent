import csv
import fcntl
import html
import io
import json
import math
import mimetypes
import multiprocessing as mp
import os
import re
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from src.api.errors import forbidden_error, not_found_error
from src.core.config import settings
from src.core.database import SessionLocal, get_db
from src.core.logging import get_logger
from src.middleware.auth import decode_token, get_current_user, security
from src.middleware.rbac import check_permission
from src.models import Agent, Document, RagDataset, User
from src.services.embedding_service import embedding_service
from src.services.qdrant_service import qdrant_service
from src.services.rag_vectorizer import iter_chunk_text, iter_chunk_text_with_pages
from src.tools.document_convert import convert_document_to_markdown

router = APIRouter()
logger = get_logger(__name__)

UPLOAD_ROOT = Path('/tmp/dyagent_uploads')
UPLOAD_STREAM_CHUNK_BYTES = 1024 * 1024
MAX_SYNC_INDEX_FILE_BYTES = 32 * 1024 * 1024
INDEX_PROGRESS_DIR_NAME = '.index-progress'
INDEXING_STALE_TIMEOUT_SECONDS = 15 * 60
INDEX_QUEUE_DIR_NAME = '.index-queue'
INDEX_QUEUE_LOCK_FILENAME = '.dispatch.lock'
INDEX_QUEUE_DEFAULT_MAX_CONCURRENCY = 1
DEFAULT_RAG_INDEX_BATCH_SIZE = 8
PDF_DOCLING_FALLBACK_MAX_FILE_BYTES = 32 * 1024 * 1024
PDF_DOCLING_LOCK_FILENAME = '.pdf-docling.lock'


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


def _dataset_progress_dir_by_dataset_id(dataset_id: str) -> Path:
    # 目的：回傳資料集索引進度檔目錄。
    # 為什麼：索引進度需與原始檔案分離保存，避免互相覆蓋。
    return UPLOAD_ROOT / 'private' / str(dataset_id) / INDEX_PROGRESS_DIR_NAME


def _dataset_progress_dir(dataset_row: RagDataset) -> Path:
    # 目的：依資料集 scope 解析索引進度檔目錄。
    # 為什麼：global 與 private 的上傳根目錄不同，需一致映射進度檔位置。
    if dataset_row.scope == 'agent_private':
        return _dataset_progress_dir_by_dataset_id(str(dataset_row.id))
    return UPLOAD_ROOT / 'global' / str(dataset_row.id) / INDEX_PROGRESS_DIR_NAME


def _dataset_progress_file_path(*, dataset_row: RagDataset, file_key: str) -> Path:
    # 目的：建立單檔索引進度檔路徑。
    # 為什麼：前端輪詢需要可穩定定位同一份上傳任務的狀態。
    return _dataset_progress_dir(dataset_row) / f'{file_key}.json'


def _write_progress_file(*, progress_file_path: str, payload: dict[str, Any]) -> None:
    # 目的：寫入索引進度檔。
    # 為什麼：背景子進程與 API 進程需透過檔案交換可觀測狀態。
    path_obj = Path(str(progress_file_path or '').strip())
    if not path_obj:
        return
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    existing_payload: dict[str, Any] = {}
    if path_obj.exists() and path_obj.is_file():
        try:
            current_payload = json.loads(path_obj.read_text(encoding='utf-8') or '{}')
            if isinstance(current_payload, dict):
                existing_payload = current_payload
        except Exception:
            existing_payload = {}
    merged_payload = {
        **existing_payload,
        **dict(payload or {}),
    }
    merged_payload['updated_at'] = datetime.now().isoformat()
    path_obj.write_text(json.dumps(merged_payload, ensure_ascii=False), encoding='utf-8')


def _read_progress_file(*, progress_file_path: str) -> dict[str, Any]:
    # 目的：讀取索引進度檔。
    # 為什麼：文件列表需回傳即時索引狀態與進度百分比。
    path_obj = Path(str(progress_file_path or '').strip())
    if not path_obj.exists() or not path_obj.is_file():
        return {}
    try:
        payload = json.loads(path_obj.read_text(encoding='utf-8') or '{}')
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_progress_stale(*, progress_payload: dict[str, Any]) -> bool:
    # 目的：判斷索引進度是否已超時停滯。
    # 為什麼：子進程若異常中止，前端會看到卡住進度，需要自動轉為 failed。
    updated_at_raw = str((progress_payload or {}).get('updated_at') or '').strip()
    if not updated_at_raw:
        return False
    try:
        updated_at = datetime.fromisoformat(updated_at_raw)
    except Exception:
        return False
    timeout_seconds = max(60, int(getattr(settings, 'RAG_INDEX_STALE_TIMEOUT_SEC', INDEXING_STALE_TIMEOUT_SECONDS) or INDEXING_STALE_TIMEOUT_SECONDS))
    return (datetime.now() - updated_at).total_seconds() > float(timeout_seconds)


def _index_queue_dir() -> Path:
    # 目的：提供背景索引佇列檔案目錄。
    # 為什麼：用檔案型佇列緩衝大量上傳，避免一次啟動過多子進程。
    return UPLOAD_ROOT / INDEX_QUEUE_DIR_NAME


def _index_queue_lock_path() -> Path:
    # 目的：提供背景索引派工鎖檔路徑。
    # 為什麼：避免多個請求同時派工造成重複啟動任務。
    return _index_queue_dir() / INDEX_QUEUE_LOCK_FILENAME


def _index_max_concurrency() -> int:
    # 目的：回傳背景索引最大並行數。
    # 為什麼：限制重任務同時執行數可防止記憶體峰值失控。
    configured = int(getattr(settings, 'RAG_INDEX_MAX_CONCURRENCY', INDEX_QUEUE_DEFAULT_MAX_CONCURRENCY) or INDEX_QUEUE_DEFAULT_MAX_CONCURRENCY)
    return max(1, min(8, configured))


def _iter_progress_files() -> Iterator[Path]:
    # 目的：列舉所有索引進度檔。
    # 為什麼：派工前需統計目前正在執行中的索引工作數量。
    if not UPLOAD_ROOT.exists() or not UPLOAD_ROOT.is_dir():
        return
    for candidate in UPLOAD_ROOT.rglob('*.json'):
        if not candidate.is_file():
            continue
        if candidate.parent.name != INDEX_PROGRESS_DIR_NAME:
            continue
        yield candidate


def _count_active_index_jobs() -> int:
    # 目的：統計目前進行中的索引工作數。
    # 為什麼：派工時必須遵守併發上限，避免同時啟動過多子進程。
    active_count = 0
    for progress_file in _iter_progress_files() or []:
        progress_payload = _read_progress_file(progress_file_path=str(progress_file))
        status = str(progress_payload.get('status') or '').strip().lower()
        if status != 'indexing':
            continue
        if _is_progress_stale(progress_payload=progress_payload):
            continue
        active_count += 1
    return active_count


def _mark_job_failed(*, progress_file_path: str, file_key: str, filename: str, error_code: str) -> None:
    # 目的：將佇列任務標記為失敗。
    # 為什麼：派工或子進程啟動失敗時，需回寫可觀測的最終狀態給前端。
    if not progress_file_path:
        return
    _write_progress_file(
        progress_file_path=progress_file_path,
        payload={
            'status': 'failed',
            'stage': 'failed',
            'progress': 100,
            'file_key': file_key,
            'filename': filename,
            'last_error': error_code,
        },
    )


def _enqueue_index_job(*, job_type: str, kwargs: dict[str, Any]) -> None:
    # 目的：將索引任務寫入佇列。
    # 為什麼：大檔上傳時先排隊可平滑負載，避免立即爆量啟動背景進程。
    queue_dir = _index_queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_payload = {
        'job_id': str(uuid.uuid4()),
        'job_type': str(job_type or '').strip(),
        'kwargs': dict(kwargs or {}),
        'enqueued_at': datetime.now().isoformat(),
    }
    job_filename = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{job_payload['job_id']}.json"
    (queue_dir / job_filename).write_text(json.dumps(job_payload, ensure_ascii=False), encoding='utf-8')


def _acquire_dispatch_lock() -> int | None:
    # 目的：嘗試取得派工互斥鎖。
    # 為什麼：同時只有一個派工者可操作佇列，避免重複取出同一任務。
    queue_dir = _index_queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    lock_path = _index_queue_lock_path()
    try:
        return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def _release_dispatch_lock(lock_fd: int | None) -> None:
    # 目的：釋放派工互斥鎖。
    # 為什麼：派工完成後必須解除鎖定，讓後續請求可接續派工。
    try:
        if isinstance(lock_fd, int):
            os.close(lock_fd)
    except Exception:
        pass
    try:
        lock_path = _index_queue_lock_path()
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        pass


def _dispatch_index_jobs_once() -> int:
    # 目的：單次派工，從佇列啟動可執行的索引任務。
    # 為什麼：限制背景索引並行數，避免多檔同時處理造成記憶體暴增。
    lock_fd = _acquire_dispatch_lock()
    if lock_fd is None:
        return 0

    started_count = 0
    try:
        queue_dir = _index_queue_dir()
        max_concurrency = _index_max_concurrency()
        active_count = _count_active_index_jobs()

        while active_count < max_concurrency:
            queue_files = sorted([path for path in queue_dir.glob('*.json') if path.name != INDEX_QUEUE_LOCK_FILENAME])
            if not queue_files:
                break

            job_file = queue_files[0]
            try:
                raw_payload = json.loads(job_file.read_text(encoding='utf-8') or '{}')
            except Exception:
                raw_payload = {}
            try:
                job_file.unlink(missing_ok=True)
            except Exception:
                pass

            job_type = str(raw_payload.get('job_type') or '').strip()
            kwargs = raw_payload.get('kwargs') if isinstance(raw_payload.get('kwargs'), dict) else {}
            progress_file_path = str(kwargs.get('progress_file_path') or '').strip()
            file_key = str(kwargs.get('file_key') or '').strip()
            filename = str(kwargs.get('filename') or '').strip()

            if job_type not in {'agent_document', 'dataset_document'}:
                _mark_job_failed(
                    progress_file_path=progress_file_path,
                    file_key=file_key,
                    filename=filename,
                    error_code='index_job_invalid_type',
                )
                continue

            target = _run_agent_document_index_process_entry if job_type == 'agent_document' else _run_dataset_document_index_process_entry
            process_name = 'rag-agent-index' if job_type == 'agent_document' else 'rag-dataset-index'

            try:
                process_id = _spawn_index_process(target=target, kwargs=kwargs, process_name=process_name)
                if progress_file_path:
                    _write_progress_file(
                        progress_file_path=progress_file_path,
                        payload={
                            'status': 'indexing',
                            'stage': 'starting',
                            'progress': 1,
                            'file_key': file_key,
                            'filename': filename,
                            'process_pid': int(process_id or 0),
                            'last_error': None,
                        },
                    )
                started_count += 1
                active_count += 1
            except Exception as error:
                _mark_job_failed(
                    progress_file_path=progress_file_path,
                    file_key=file_key,
                    filename=filename,
                    error_code=f'index_process_spawn_failed:{error.__class__.__name__}',
                )
                continue
    finally:
        _release_dispatch_lock(lock_fd)

    return started_count


def _is_process_alive(process_pid: int | None) -> bool | None:
    # 目的：判斷索引子進程是否仍在執行。
    # 為什麼：避免僅靠時間判斷卡住，對長時間抽取任務產生誤判。
    if not isinstance(process_pid, int) or process_pid <= 0:
        return None
    try:
        os.kill(process_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _parse_dataset_file_key(file_key: str) -> str:
    # 目的：正規化資料集文件的唯一鍵。
    # 為什麼：阻擋路徑穿越與非法字元，確保刪除操作只作用於指定上傳版本。
    cleaned_key = str(file_key or '').strip()
    if not cleaned_key:
        return ''
    if '/' in cleaned_key or '\\' in cleaned_key:
        return ''
    if '..' in cleaned_key:
        return ''
    return cleaned_key


def _display_name_from_stored_key(stored_key: str) -> str:
    # 目的：從唯一鍵回推原始展示檔名。
    # 為什麼：上傳時會加 UUID 前綴，列表顯示需保留使用者可辨識名稱。
    normalized_stored_key = str(stored_key or '').strip()
    if '_' in normalized_stored_key:
        return normalized_stored_key.split('_', 1)[1]
    return normalized_stored_key


def _resolve_dataset_document_context(
    *,
    dataset_id: str,
    file_key: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: Session,
) -> tuple[RagDataset, Path]:
    # 目的：解析資料集文件開啟請求並完成權限檢查。
    # 為什麼：open / open-preview 共享同一套驗證邏輯，集中維護可避免權限漂移。
    try:
        dataset_uuid = uuid.UUID(str(dataset_id))
    except Exception:
        raise not_found_error('RagDataset', dataset_id) from None

    normalized_file_key = _parse_dataset_file_key(file_key)
    if not normalized_file_key:
        raise not_found_error('Document', file_key)

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid).first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    current_user: User | None = None
    token_text = str(getattr(credentials, 'credentials', '') or '').strip()
    if not token_text:
        token_text = str(request.query_params.get('access_token') or '').strip()
    if token_text:
        payload = decode_token(token_text)
        user_id = str(payload.get('sub') or '').strip()
        if user_id:
            current_user = db.query(User).filter(User.id == user_id).first()

    if row.scope == 'global':
        is_public_readable = bool(row.enabled) and str(row.sensitivity or '') == 'normal'
        if current_user is None:
            if not is_public_readable:
                raise forbidden_error('Authentication required')
        else:
            can_chat = check_permission(current_user, 'chat')
            can_read_agent = check_permission(current_user, 'read_agent')
            if not (current_user.role == 'admin' or (is_public_readable and (can_chat or can_read_agent))):
                raise forbidden_error()
    elif row.scope == 'agent_private':
        if current_user is None:
            raise forbidden_error('Authentication required')
        if not check_permission(current_user, 'read_agent'):
            raise forbidden_error()
    else:
        raise forbidden_error()

    upload_dir = _dataset_upload_dir(row)
    target_path = upload_dir / normalized_file_key
    if not target_path.exists() or not target_path.is_file():
        raise not_found_error('Document', normalized_file_key)
    return row, target_path


async def _persist_upload_to_path(*, uploaded_file_obj: Any, save_path: Path) -> int:
    # 目的：以串流方式保存上傳文件並回傳檔案大小。
    # 為什麼：避免一次 read() 把大檔全部放進記憶體，造成上傳時 RAM 暴增。
    total_size_bytes = 0
    with open(save_path, 'wb') as output_handle:
        while True:
            chunk_bytes = await uploaded_file_obj.read(UPLOAD_STREAM_CHUNK_BYTES)
            if not chunk_bytes:
                break
            output_handle.write(chunk_bytes)
            total_size_bytes += len(chunk_bytes)
    return total_size_bytes


def _extract_pdf_text_with_pypdf(content: bytes) -> tuple[str, str | None]:
    # 目的：以 pypdf 解析 PDF 文字層。
    # 為什麼：pypdf 速度快且可直接處理純文字 PDF，作為第一層抽取器可降低延遲。
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages:
            pages.append((page.extract_text() or '').strip())
        merged_text = '\n'.join([page_text for page_text in pages if page_text])
        if not merged_text.strip():
            return '', 'pdf_empty_text_from_pypdf'
        return merged_text, None
    except Exception as error:
        return '', f'pdf_extraction_failed_or_missing_pypdf:{error.__class__.__name__}'


def _extract_pdf_text_with_pypdf_from_path(file_path: str) -> tuple[str, str | None]:
    # 目的：以檔案路徑解析 PDF 文字層。
    # 為什麼：避免先把整份 PDF 載入 bytes，降低大檔索引記憶體峰值。
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        pages: list[str] = []
        for page in reader.pages:
            pages.append((page.extract_text() or '').strip())
        merged_text = '\n'.join([page_text for page_text in pages if page_text])
        if not merged_text.strip():
            return '', 'pdf_empty_text_from_pypdf'
        return merged_text, None
    except Exception as error:
        return '', f'pdf_extraction_failed_or_missing_pypdf:{error.__class__.__name__}'


def _extract_pdf_page_texts_with_pypdf(content: bytes) -> tuple[list[str], str | None]:
    # 目的：以 pypdf 擷取每頁文字內容。
    # 為什麼：保留 chunk 與頁碼的對應，供檢索結果顯示來源頁數。
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages = []
        for page in reader.pages:
            pages.append((page.extract_text() or '').strip())

        has_text = any((page_text or '').strip() for page_text in pages)
        if not has_text:
            return [], 'pdf_empty_text_from_pypdf'
        return pages, None
    except Exception as error:
        return [], f'pdf_extraction_failed_or_missing_pypdf:{error.__class__.__name__}'


def _extract_pdf_page_texts_with_pypdf_from_path(file_path: str) -> tuple[list[str], str | None]:
    # 目的：以檔案路徑擷取 PDF 每頁文字內容。
    # 為什麼：背景索引需逐頁切塊，但不應先把整份 PDF 載入記憶體。
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        pages: list[str] = []
        for page in reader.pages:
            pages.append((page.extract_text() or '').strip())

        has_text = any((page_text or '').strip() for page_text in pages)
        if not has_text:
            return [], 'pdf_empty_text_from_pypdf'
        return pages, None
    except Exception as error:
        return [], f'pdf_extraction_failed_or_missing_pypdf:{error.__class__.__name__}'


def _extract_pdf_text_with_docling(file_path: str | None) -> tuple[str, str | None]:
    # 目的：以 docling 做 PDF 備援抽取。
    # 為什麼：處理部分 pypdf 無法抽字的版面或字型映射，提升可索引成功率。
    normalized_path = str(file_path or '').strip()
    if not normalized_path:
        return '', 'pdf_docling_file_path_missing'
    markdown, error = convert_document_to_markdown(normalized_path)
    if error:
        return '', f'pdf_docling_fallback_failed:{error}'
    return markdown, None


def _resolve_pdf_docling_fallback_max_file_bytes() -> int:
    # 目的：解析 PDF docling 備援抽取的檔案大小上限。
    # 為什麼：docling 對超大 PDF 記憶體需求高，需限制其觸發條件避免 RAM 峰值失控。
    try:
        configured_bytes = int(getattr(settings, 'RAG_PDF_DOCLING_MAX_FILE_BYTES', PDF_DOCLING_FALLBACK_MAX_FILE_BYTES) or PDF_DOCLING_FALLBACK_MAX_FILE_BYTES)
    except Exception:
        configured_bytes = PDF_DOCLING_FALLBACK_MAX_FILE_BYTES
    return max(0, configured_bytes)


def _should_use_pdf_docling_fallback(file_path: str) -> bool:
    # 目的：判斷是否允許執行 docling PDF 備援抽取。
    # 為什麼：大型 PDF 先使用 pypdf 逐頁抽取可顯著降低記憶體佔用。
    max_file_bytes = _resolve_pdf_docling_fallback_max_file_bytes()
    if max_file_bytes <= 0:
        return False
    try:
        file_size_bytes = Path(str(file_path or '').strip()).stat().st_size
    except Exception:
        return True
    return int(file_size_bytes or 0) <= max_file_bytes


def _pdf_docling_lock_path() -> Path:
    # 目的：提供 docling 備援抽取互斥鎖檔路徑。
    # 為什麼：大 PDF 的 docling 抽取很耗記憶體，需要跨進程限制同時僅一個任務執行。
    return UPLOAD_ROOT / PDF_DOCLING_LOCK_FILENAME


def _acquire_pdf_docling_lock() -> Any:
    # 目的：取得 docling 備援抽取互斥鎖。
    # 為什麼：即使索引併發是 2，也要避免兩個大 PDF 同時進入 docling 導致 RAM 峰值翻倍。
    lock_path = _pdf_docling_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = open(lock_path, 'a+', encoding='utf-8')
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    return lock_handle


def _release_pdf_docling_lock(lock_handle: Any | None) -> None:
    # 目的：釋放 docling 備援抽取互斥鎖。
    # 為什麼：確保後續排隊任務可接續執行，避免互斥鎖長時間占用。
    if lock_handle is None:
        return
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        lock_handle.close()
    except Exception:
        pass


def _extract_text_for_indexing(
    content: bytes,
    content_type: str,
    filename: str,
    file_path: str | None = None,
) -> tuple[str, str | None]:
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
        pypdf_text, pypdf_error = _extract_pdf_text_with_pypdf(content)
        if pypdf_text.strip():
            return pypdf_text, None

        docling_text, docling_error = _extract_pdf_text_with_docling(file_path)
        if docling_text.strip():
            return docling_text, None

        if pypdf_error and docling_error:
            return '', f'pdf_all_extractors_failed:{pypdf_error}|{docling_error}'
        if pypdf_error:
            return '', pypdf_error
        if docling_error:
            return '', docling_error
        return '', 'pdf_empty_text_extracted'

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


def _extract_text_for_indexing_from_path(
    *,
    file_path: str,
    content_type: str,
    filename: str,
) -> tuple[str, str | None]:
    # 目的：以路徑為主抽取索引文字，必要時才讀取 bytes。
    # 為什麼：大檔若先 read_bytes 再 decode 會產生重複記憶體佔用，需減少峰值。
    normalized_path = str(file_path or '').strip()
    if not normalized_path:
        return '', 'missing_file_path'

    ct = (content_type or '').lower().split(';')[0].strip()
    ext = (Path(filename or '').suffix or '').lower()
    text_types = {
        'text/plain',
        'text/markdown',
        'application/json',
        'text/csv',
        'application/xml',
        'text/html',
    }

    def _read_text_file() -> str:
        return Path(normalized_path).read_text(encoding='utf-8', errors='ignore')

    if ct in text_types or ext in {'.txt', '.md', '.json', '.csv', '.xml', '.html', '.htm'}:
        try:
            text = _read_text_file()
        except Exception as error:
            return '', f'text_file_read_failed:{error.__class__.__name__}'

        if ct == 'text/html' or ext in {'.html', '.htm'}:
            text = re.sub(r'<script[\s\S]*?</script>', ' ', text, flags=re.IGNORECASE)
            text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            return re.sub(r'\s+', ' ', text).strip(), None

        if ct == 'application/xml' or ext == '.xml':
            text = re.sub(r'<[^>]+>', ' ', text)
            return re.sub(r'\s+', ' ', text).strip(), None

        if ct == 'application/json' or ext == '.json':
            try:
                payload = json.loads(text or '{}')
                return json.dumps(payload, ensure_ascii=False, indent=2), None
            except Exception:
                return text, None

        if ct == 'text/csv' or ext == '.csv':
            try:
                reader = csv.reader(io.StringIO(text))
                rows = ['\t'.join(row) for row in reader]
                return '\n'.join(rows), None
            except Exception:
                return text, None

        return text, None

    if ext == '.pdf':
        pypdf_text, pypdf_error = _extract_pdf_text_with_pypdf_from_path(normalized_path)
        if pypdf_text.strip():
            return pypdf_text, None

        docling_text = ''
        docling_error = None
        if _should_use_pdf_docling_fallback(normalized_path):
            docling_text, docling_error = _extract_pdf_text_with_docling(normalized_path)
            if docling_text.strip():
                return docling_text, None
        else:
            docling_error = 'pdf_docling_skipped_for_large_file'

        if pypdf_error and docling_error:
            return '', f'pdf_all_extractors_failed:{pypdf_error}|{docling_error}'
        if pypdf_error:
            return '', pypdf_error
        if docling_error:
            return '', docling_error
        return '', 'pdf_empty_text_extracted'

    try:
        binary_content = Path(normalized_path).read_bytes()
    except Exception as error:
        return '', f'binary_file_read_failed:{error.__class__.__name__}'

    return _extract_text_for_indexing(binary_content, content_type, filename, normalized_path)


def _iter_chunks_for_indexing(
    *,
    text: str,
    filename: str,
    content: bytes | None = None,
    file_path: str | None = None,
) -> Iterator[dict[str, int | str | None]]:
    # 目的：建立可索引切塊並補齊頁碼資訊（惰性輸出）。
    # 為什麼：大型文件若一次建立完整 chunk 清單，會放大記憶體峰值。
    ext = (Path(filename or '').suffix or '').lower()
    if ext == '.pdf':
        normalized_file_path = str(file_path or '').strip()
        if normalized_file_path:
            try:
                from pypdf import PdfReader

                reader = PdfReader(normalized_file_path)
                for page_number, page in enumerate(reader.pages, start=1):
                    page_text = (page.extract_text() or '').strip()
                    if not page_text:
                        continue
                    for chunk in iter_chunk_text(page_text):
                        normalized_chunk = str(chunk or '').strip()
                        if not normalized_chunk:
                            continue
                        yield {
                            'text': normalized_chunk,
                            'page_number': page_number,
                        }
                return
            except Exception:
                pass

        page_texts: list[str] = []
        if isinstance(content, bytes):
            page_texts, _page_error = _extract_pdf_page_texts_with_pypdf(content)
        if page_texts:
            for item in iter_chunk_text_with_pages(page_texts):
                yield {
                    'text': str(item.get('text') or ''),
                    'page_number': item.get('page_number') if isinstance(item.get('page_number'), int) else None,
                }
            return

    for chunk in iter_chunk_text(text):
        normalized_chunk = str(chunk or '').strip()
        if not normalized_chunk:
            continue
        yield {
            'text': normalized_chunk,
            'page_number': None,
        }


def _build_chunks_for_indexing(*, text: str, filename: str, content: bytes) -> list[dict[str, int | str | None]]:
    # 目的：建立可索引切塊並補齊頁碼資訊。
    # 為什麼：索引與頁碼對應應集中在單一路徑，避免不同上傳端點行為不一致。
    return list(_iter_chunks_for_indexing(text=text, filename=filename, content=content))


def _resolve_index_batch_size() -> int:
    # 目的：解析索引批次大小設定。
    # 為什麼：向量化與寫入批次需可調整，平衡效能與記憶體壓力。
    try:
        configured_size = int(getattr(settings, 'RAG_INDEX_BATCH_SIZE', DEFAULT_RAG_INDEX_BATCH_SIZE) or DEFAULT_RAG_INDEX_BATCH_SIZE)
    except Exception:
        configured_size = DEFAULT_RAG_INDEX_BATCH_SIZE
    return max(1, min(256, configured_size))


def _ensure_embedding_ready_for_indexing() -> tuple[bool, str | None]:
    # 目的：索引前確認 embedding provider 可用。
    # 為什麼：避免先跑昂貴抽取流程才在向量化階段失敗。
    try:
        return embedding_service.preflight_for_bulk_index()
    except Exception as error:
        return False, f'embedding_provider_unavailable:unexpected:{error.__class__.__name__}'


def _ensure_qdrant_ready_for_indexing() -> tuple[bool, str | None]:
    # 目的：索引前確認 Qdrant 可連線。
    # 為什麼：避免先跑耗時 OCR 後，才因向量庫不可用導致整批失敗。
    try:
        if qdrant_service.client is None:
            qdrant_service.connect()
        if qdrant_service.client is None:
            return False, 'vector_store_unavailable:qdrant_connect_failed'
        qdrant_service.client.get_collections()
        return True, None
    except Exception as error:
        return False, f'vector_store_unavailable:qdrant:{error.__class__.__name__}'


def _index_document_chunks(
    *,
    agent_id: str,
    document_id: str,
    filename: str,
    file_key: str,
    chunks_with_meta: Iterable[dict[str, int | str | None]],
    progress_reporter: Any | None = None,
) -> tuple[bool, str | None]:
    # 目的：將文件切塊並寫入向量庫。
    # 為什麼：讓上傳後可立即被 RAG 檢索，縮短配置到可用的路徑。
    collection = _agent_collection_name(agent_id)
    batch_size = _resolve_index_batch_size()
    buffer: list[dict[str, int | str | None]] = []
    next_chunk_index = 0
    indexed_count = 0
    collection_ready = False

    def _flush_batch(rows: list[dict[str, int | str | None]], start_index: int, ready: bool) -> tuple[bool, bool, int, str | None]:
        # 目的：批次向量化並寫入 Qdrant。
        # 為什麼：避免將所有 vectors/payloads 一次放在記憶體，造成高峰值。
        texts = [str((row or {}).get('text') or '').strip() for row in rows]
        if not texts:
            return True, ready, 0, None

        vectors = embedding_service.embed_texts(texts, allow_fallback=False)
        if len(vectors) != len(texts):
            return False, ready, 0, f'embedding_count_mismatch:expected={len(texts)}:actual={len(vectors)}'
        if not vectors:
            return False, ready, 0, 'embedding_empty'

        expected_size = len(vectors[0])
        existing_size = qdrant_service.get_collection_vector_size(collection_name=collection)
        if isinstance(existing_size, int) and existing_size > 0 and existing_size != expected_size:
            return False, ready, 0, f'vector_size_mismatch:collection={collection}:existing={existing_size}:expected={expected_size}'

        if not ready:
            ready = bool(qdrant_service.create_collection(collection_name=collection, vector_size=expected_size))
            if not ready:
                return False, ready, 0, f'collection_create_failed:{collection}'

        payloads: list[dict[str, Any]] = []
        ids: list[str] = []
        for offset, row in enumerate(rows):
            chunk_index = start_index + offset
            chunk = str((row or {}).get('text') or '')
            page_number_raw = (row or {}).get('page_number')
            page_number = int(page_number_raw) if isinstance(page_number_raw, int) else None
            payload = {
                'document_id': document_id,
                'agent_id': str(agent_id),
                'filename': filename,
                'file_key': file_key,
                'chunk_index': chunk_index,
                'snippet': chunk[:400],
            }
            if page_number is not None:
                payload['page_number'] = page_number
            payloads.append(payload)
            ids.append(f'{document_id}-{chunk_index}')

        upsert_ok = bool(qdrant_service.upsert_vectors(
            collection_name=collection,
            vectors=vectors,
            payloads=payloads,
            ids=ids,
        ))
        if not upsert_ok:
            return False, ready, 0, f'upsert_failed:{collection}:batch={len(rows)}'
        return True, ready, len(rows), None

    for chunk_row in chunks_with_meta:
        text = str((chunk_row or {}).get('text') or '').strip()
        if not text:
            continue
        page_number_raw = (chunk_row or {}).get('page_number')
        page_number = int(page_number_raw) if isinstance(page_number_raw, int) else None
        buffer.append({'text': text, 'page_number': page_number})
        if len(buffer) < batch_size:
            continue

        ok, collection_ready, flushed_count, reason = _flush_batch(buffer, next_chunk_index, collection_ready)
        if not ok:
            return False, reason
        indexed_count += flushed_count
        next_chunk_index += flushed_count
        if callable(progress_reporter):
            progress_reporter(indexed_count)
        buffer = []

    if buffer:
        ok, collection_ready, flushed_count, reason = _flush_batch(buffer, next_chunk_index, collection_ready)
        if not ok:
            return False, reason
        indexed_count += flushed_count
        if callable(progress_reporter):
            progress_reporter(indexed_count)

    if indexed_count <= 0:
        return False, 'no_valid_chunks'
    return True, None


def _index_dataset_chunks(
    *,
    collection_name: str,
    dataset_id: str,
    filename: str,
    file_key: str,
    chunks_with_meta: Iterable[dict[str, int | str | None]],
    progress_reporter: Any | None = None,
) -> tuple[bool, str | None]:
    # 目的：將公有資料集文件切塊並寫入向量庫。
    # 為什麼：讓公有資料集能由管理頁上傳後立即被綁定代理者使用。
    batch_size = _resolve_index_batch_size()
    buffer: list[dict[str, int | str | None]] = []
    next_chunk_index = 0
    indexed_count = 0
    collection_ready = False

    def _flush_batch(rows: list[dict[str, int | str | None]], start_index: int, ready: bool) -> tuple[bool, bool, int, str | None]:
        # 目的：批次向量化並寫入資料集集合。
        # 為什麼：大量切塊若一次 upsert，記憶體峰值會快速放大。
        texts = [str((row or {}).get('text') or '').strip() for row in rows]
        if not texts:
            return True, ready, 0, None

        vectors = embedding_service.embed_texts(texts, allow_fallback=False)
        if len(vectors) != len(texts):
            return False, ready, 0, f'embedding_count_mismatch:expected={len(texts)}:actual={len(vectors)}'
        if not vectors:
            return False, ready, 0, 'embedding_empty'

        expected_size = len(vectors[0])
        existing_size = qdrant_service.get_collection_vector_size(collection_name=collection_name)
        if isinstance(existing_size, int) and existing_size > 0 and existing_size != expected_size:
            return False, ready, 0, f'vector_size_mismatch:collection={collection_name}:existing={existing_size}:expected={expected_size}'

        if not ready:
            ready = bool(qdrant_service.create_collection(collection_name=collection_name, vector_size=expected_size))
            if not ready:
                return False, ready, 0, f'collection_create_failed:{collection_name}'

        payloads: list[dict[str, Any]] = []
        ids: list[str] = []
        for offset, row in enumerate(rows):
            chunk_index = start_index + offset
            chunk = str((row or {}).get('text') or '')
            page_number_raw = (row or {}).get('page_number')
            page_number = int(page_number_raw) if isinstance(page_number_raw, int) else None
            payload = {
                'dataset_id': dataset_id,
                'filename': filename,
                'file_key': file_key,
                'chunk_index': chunk_index,
                'snippet': chunk[:400],
                'scope': 'global',
            }
            if page_number is not None:
                payload['page_number'] = page_number
            payloads.append(payload)
            ids.append(str(uuid.uuid4()))

        upsert_ok = bool(qdrant_service.upsert_vectors(
            collection_name=collection_name,
            vectors=vectors,
            payloads=payloads,
            ids=ids,
        ))
        if not upsert_ok:
            return False, ready, 0, f'upsert_failed:{collection_name}:batch={len(rows)}'
        return True, ready, len(rows), None

    for chunk_row in chunks_with_meta:
        text = str((chunk_row or {}).get('text') or '').strip()
        if not text:
            continue
        page_number_raw = (chunk_row or {}).get('page_number')
        page_number = int(page_number_raw) if isinstance(page_number_raw, int) else None
        buffer.append({'text': text, 'page_number': page_number})
        if len(buffer) < batch_size:
            continue

        ok, collection_ready, flushed_count, reason = _flush_batch(buffer, next_chunk_index, collection_ready)
        if not ok:
            return False, reason
        indexed_count += flushed_count
        next_chunk_index += flushed_count
        if callable(progress_reporter):
            progress_reporter(indexed_count)
        buffer = []

    if buffer:
        ok, collection_ready, flushed_count, reason = _flush_batch(buffer, next_chunk_index, collection_ready)
        if not ok:
            return False, reason
        indexed_count += flushed_count
        if callable(progress_reporter):
            progress_reporter(indexed_count)

    if indexed_count <= 0:
        return False, 'no_valid_chunks'
    return True, None


def _resolve_document_status(*, current_status: str | None, file_exists: bool) -> str:
    # 目的：回傳可對外展示的文件狀態。
    # 為什麼：歷史資料可能沒有 status 欄位，需有穩健回退避免前端顯示不一致。
    status = str(current_status or '').strip().lower()
    if status in {'uploaded', 'queued', 'indexing', 'ready', 'failed'}:
        return status
    return 'ready' if file_exists else 'missing'


def _run_agent_document_background_index(
    *,
    agent_id: str,
    document_id: str,
    filename: str,
    file_key: str,
    file_path: str,
    content_type: str,
    progress_file_path: str | None = None,
) -> None:
    # 目的：在背景執行大檔文件索引並回寫文件狀態。
    # 為什麼：避免同步請求讀取大檔造成高記憶體與逾時，改由回應後非阻塞處理。
    db = SessionLocal()
    if progress_file_path:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'embedding_preflight',
                'progress': 2,
                'file_key': file_key,
                'filename': filename,
            },
        )

    is_embedding_ready, embedding_error = _ensure_embedding_ready_for_indexing()
    if not is_embedding_ready:
        try:
            target_document = db.query(Document).filter(Document.id == document_id, Document.agent_id == agent_id).first()
            if target_document:
                target_document.status = 'failed'
                target_document.last_error = str(embedding_error or 'embedding_provider_unavailable')
                target_document.indexed_at = None
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'failed',
                    'stage': 'failed',
                    'progress': 100,
                    'file_key': file_key,
                    'filename': filename,
                    'last_error': str(embedding_error or 'embedding_provider_unavailable'),
                },
            )
        return

    is_qdrant_ready, qdrant_error = _ensure_qdrant_ready_for_indexing()
    if not is_qdrant_ready:
        try:
            target_document = db.query(Document).filter(Document.id == document_id, Document.agent_id == agent_id).first()
            if target_document:
                target_document.status = 'failed'
                target_document.last_error = str(qdrant_error or 'vector_store_unavailable')
                target_document.indexed_at = None
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'failed',
                    'stage': 'failed',
                    'progress': 100,
                    'file_key': file_key,
                    'filename': filename,
                    'last_error': str(qdrant_error or 'vector_store_unavailable'),
                },
            )
        return

    if progress_file_path:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'extracting',
                'progress': 5,
                'file_key': file_key,
                'filename': filename,
            },
        )
    extracted_text = ''
    extract_error = None
    file_extension = (Path(filename or '').suffix or '').lower()
    if file_extension != '.pdf':
        try:
            extracted_text, extract_error = _extract_text_for_indexing_from_path(
                file_path=file_path,
                content_type=content_type,
                filename=filename,
            )
        except Exception as error:
            try:
                target_document = db.query(Document).filter(Document.id == document_id, Document.agent_id == agent_id).first()
                if target_document:
                    target_document.status = 'failed'
                    target_document.last_error = f'background_index_file_read_failed:{error.__class__.__name__}'
                    target_document.indexed_at = None
                    db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
            return

    if progress_file_path:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'chunking',
                'progress': 25,
                'file_key': file_key,
                'filename': filename,
            },
        )

    estimated_total_chunks = 1
    if file_extension != '.pdf':
        estimated_total_chunks = max(1, int(math.ceil(len(extracted_text) / 800)))
    else:
        try:
            file_size_bytes = Path(str(file_path or '').strip()).stat().st_size
        except Exception:
            file_size_bytes = 0
        estimated_total_chunks = max(1, int(math.ceil(float(file_size_bytes) / float(256 * 1024))))

    def _report_index_progress(processed_chunks: int) -> None:
        if not progress_file_path:
            return
        ratio = min(1.0, max(0.0, float(processed_chunks) / float(estimated_total_chunks)))
        mapped_progress = int(35 + (ratio * 60))
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'embedding_upsert',
                'progress': mapped_progress,
                'file_key': file_key,
                'filename': filename,
                'processed_chunks': int(processed_chunks),
                'estimated_total_chunks': int(estimated_total_chunks),
            },
        )

    chunks_with_meta = _iter_chunks_for_indexing(
        text=extracted_text,
        filename=filename,
        content=None,
        file_path=file_path,
    )
    if file_extension != '.pdf' and not extracted_text.strip():
        status = 'failed'
        last_error = extract_error or 'unsupported_or_empty_content'
        indexed_at = None
    else:
        indexed, index_error = _index_document_chunks(
            agent_id=agent_id,
            document_id=document_id,
            filename=filename,
            file_key=file_key,
            chunks_with_meta=chunks_with_meta,
            progress_reporter=_report_index_progress,
        )
        if not indexed and file_extension == '.pdf' and str(index_error or '') == 'no_valid_chunks':
            docling_lock = None
            if progress_file_path:
                _write_progress_file(
                    progress_file_path=progress_file_path,
                    payload={
                        'status': 'indexing',
                        'stage': 'docling_wait',
                        'progress': 30,
                        'file_key': file_key,
                        'filename': filename,
                    },
                )
            try:
                docling_lock = _acquire_pdf_docling_lock()
                if progress_file_path:
                    _write_progress_file(
                        progress_file_path=progress_file_path,
                        payload={
                            'status': 'indexing',
                            'stage': 'docling_extracting',
                            'progress': 33,
                            'file_key': file_key,
                            'filename': filename,
                        },
                    )
                docling_text, docling_error = _extract_pdf_text_with_docling(file_path)
            finally:
                _release_pdf_docling_lock(docling_lock)
            if docling_text.strip():
                fallback_chunks = _iter_chunks_for_indexing(
                    text=docling_text,
                    filename='fallback.txt',
                    content=None,
                    file_path=None,
                )
                indexed, index_error = _index_document_chunks(
                    agent_id=agent_id,
                    document_id=document_id,
                    filename=filename,
                    file_key=file_key,
                    chunks_with_meta=fallback_chunks,
                    progress_reporter=_report_index_progress,
                )
            else:
                index_error = docling_error or 'pdf_docling_fallback_failed'
        status = 'ready' if indexed else 'failed'
        last_error = None if indexed else (index_error or extract_error or 'index_upsert_failed')
        indexed_at = datetime.now() if indexed else None

    try:
        target_document = db.query(Document).filter(Document.id == document_id, Document.agent_id == agent_id).first()
        if target_document:
            target_document.status = status
            target_document.last_error = last_error
            target_document.indexed_at = indexed_at
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    if progress_file_path:
        final_progress = 100 if status == 'ready' else 100
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': status,
                'stage': 'done' if status == 'ready' else 'failed',
                'progress': final_progress,
                'file_key': file_key,
                'filename': filename,
                'last_error': last_error,
                'indexed_at': indexed_at.isoformat() if indexed_at else None,
            },
        )


def _run_dataset_document_background_index(
    *,
    collection_name: str,
    dataset_id: str,
    filename: str,
    file_key: str,
    file_path: str,
    content_type: str,
    progress_file_path: str | None = None,
) -> None:
    # 目的：在背景執行資料集大檔索引。
    # 為什麼：資料集文件不應因大檔同步解析拖慢管理介面，需在回應後持續處理。
    if progress_file_path:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'embedding_preflight',
                'progress': 2,
                'file_key': file_key,
                'filename': filename,
            },
        )

    is_embedding_ready, embedding_error = _ensure_embedding_ready_for_indexing()
    if not is_embedding_ready:
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'failed',
                    'stage': 'failed',
                    'progress': 100,
                    'file_key': file_key,
                    'filename': filename,
                    'last_error': str(embedding_error or 'embedding_provider_unavailable'),
                },
            )
        logger.warning(
            'rag.dataset_background_index.embedding_unavailable',
            dataset_id=dataset_id,
            file_key=file_key,
            error=str(embedding_error or 'embedding_provider_unavailable'),
        )
        return

    is_qdrant_ready, qdrant_error = _ensure_qdrant_ready_for_indexing()
    if not is_qdrant_ready:
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'failed',
                    'stage': 'failed',
                    'progress': 100,
                    'file_key': file_key,
                    'filename': filename,
                    'last_error': str(qdrant_error or 'vector_store_unavailable'),
                },
            )
        logger.warning(
            'rag.dataset_background_index.vector_store_unavailable',
            dataset_id=dataset_id,
            file_key=file_key,
            error=str(qdrant_error or 'vector_store_unavailable'),
        )
        return

    if progress_file_path:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'extracting',
                'progress': 5,
                'file_key': file_key,
                'filename': filename,
            },
        )

    extracted_text = ''
    extract_error = None
    file_extension = (Path(filename or '').suffix or '').lower()
    if file_extension != '.pdf':
        try:
            extracted_text, extract_error = _extract_text_for_indexing_from_path(
                file_path=file_path,
                content_type=content_type,
                filename=filename,
            )
        except Exception as error:
            failure_reason = f'background_index_file_read_failed:{error.__class__.__name__}'
            logger.warning(
                'rag.dataset_background_index.extract_failed',
                dataset_id=dataset_id,
                file_key=file_key,
                error=failure_reason,
            )
            if progress_file_path:
                _write_progress_file(
                    progress_file_path=progress_file_path,
                    payload={
                        'status': 'failed',
                        'stage': 'failed',
                        'progress': 100,
                        'file_key': file_key,
                        'filename': filename,
                        'last_error': failure_reason,
                    },
                )
            return

    if progress_file_path:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'chunking',
                'progress': 25,
                'file_key': file_key,
                'filename': filename,
            },
        )

    estimated_total_chunks = 1
    if file_extension != '.pdf':
        estimated_total_chunks = max(1, int(math.ceil(len(extracted_text) / 800)))
    else:
        try:
            file_size_bytes = Path(str(file_path or '').strip()).stat().st_size
        except Exception:
            file_size_bytes = 0
        estimated_total_chunks = max(1, int(math.ceil(float(file_size_bytes) / float(256 * 1024))))

    def _report_index_progress(processed_chunks: int) -> None:
        if not progress_file_path:
            return
        ratio = min(1.0, max(0.0, float(processed_chunks) / float(estimated_total_chunks)))
        mapped_progress = int(35 + (ratio * 60))
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'indexing',
                'stage': 'embedding_upsert',
                'progress': mapped_progress,
                'file_key': file_key,
                'filename': filename,
                'processed_chunks': int(processed_chunks),
                'estimated_total_chunks': int(estimated_total_chunks),
            },
        )

    chunks_with_meta = _iter_chunks_for_indexing(
        text=extracted_text,
        filename=filename,
        content=None,
        file_path=file_path,
    )
    if file_extension != '.pdf' and not extracted_text.strip():
        logger.warning('rag.dataset_background_index.no_chunks', dataset_id=dataset_id, file_key=file_key)
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'failed',
                    'stage': 'failed',
                    'progress': 100,
                    'file_key': file_key,
                    'filename': filename,
                    'last_error': extract_error or 'unsupported_or_empty_content',
                },
            )
        return

    indexed, index_error = _index_dataset_chunks(
        collection_name=collection_name,
        dataset_id=dataset_id,
        filename=filename,
        file_key=file_key,
        chunks_with_meta=chunks_with_meta,
        progress_reporter=_report_index_progress,
    )
    if not indexed and file_extension == '.pdf' and str(index_error or '') == 'no_valid_chunks':
        docling_lock = None
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'indexing',
                    'stage': 'docling_wait',
                    'progress': 30,
                    'file_key': file_key,
                    'filename': filename,
                },
            )
        try:
            docling_lock = _acquire_pdf_docling_lock()
            if progress_file_path:
                _write_progress_file(
                    progress_file_path=progress_file_path,
                    payload={
                        'status': 'indexing',
                        'stage': 'docling_extracting',
                        'progress': 33,
                        'file_key': file_key,
                        'filename': filename,
                    },
                )
            docling_text, docling_error = _extract_pdf_text_with_docling(file_path)
        finally:
            _release_pdf_docling_lock(docling_lock)
        if docling_text.strip():
            fallback_chunks = _iter_chunks_for_indexing(
                text=docling_text,
                filename='fallback.txt',
                content=None,
                file_path=None,
            )
            indexed, index_error = _index_dataset_chunks(
                collection_name=collection_name,
                dataset_id=dataset_id,
                filename=filename,
                file_key=file_key,
                chunks_with_meta=fallback_chunks,
                progress_reporter=_report_index_progress,
            )
        else:
            index_error = docling_error or 'pdf_docling_fallback_failed'
    if not indexed:
        failure_reason = str(index_error or extract_error or 'index_failed')
        logger.warning('rag.dataset_background_index.index_failed', dataset_id=dataset_id, file_key=file_key, error=failure_reason)
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'failed',
                    'stage': 'failed',
                    'progress': 100,
                    'file_key': file_key,
                    'filename': filename,
                    'last_error': failure_reason,
                },
            )
        return

    if progress_file_path:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'ready',
                'stage': 'done',
                'progress': 100,
                'file_key': file_key,
                'filename': filename,
                'indexed_at': datetime.now().isoformat(),
                'last_error': None,
            },
        )


def _spawn_index_process(*, target: Any, kwargs: dict[str, Any], process_name: str) -> int:
    # 目的：以獨立進程執行大型索引任務。
    # 為什麼：FastAPI BackgroundTasks 與 API 共用進程，無法隔離高記憶體峰值風險。
    process_context = mp.get_context('spawn')
    process = process_context.Process(target=target, kwargs=kwargs, daemon=True, name=process_name)
    process.start()
    return int(process.pid or 0)


def _run_agent_document_index_process_entry(**kwargs: Any) -> None:
    # 目的：代理者文件索引子進程入口。
    # 為什麼：集中子進程錯誤保護，避免未捕獲例外導致靜默失敗。
    try:
        _run_agent_document_background_index(**kwargs)
    except Exception as error:
        logger.error('rag.agent_index_process_failed', error=str(error), kwargs=kwargs)


def _run_dataset_document_index_process_entry(**kwargs: Any) -> None:
    # 目的：資料集文件索引子進程入口。
    # 為什麼：將非同步回應後的重任務移出 API 主進程，降低崩潰風險。
    try:
        _run_dataset_document_background_index(**kwargs)
    except Exception as error:
        logger.error('rag.dataset_index_process_failed', error=str(error), kwargs=kwargs)
        progress_file_path = str((kwargs or {}).get('progress_file_path') or '').strip()
        if progress_file_path:
            _write_progress_file(
                progress_file_path=progress_file_path,
                payload={
                    'status': 'failed',
                    'stage': 'failed',
                    'progress': 100,
                    'file_key': str((kwargs or {}).get('file_key') or ''),
                    'filename': str((kwargs or {}).get('filename') or ''),
                    'last_error': f'index_process_failed:{error.__class__.__name__}',
                },
            )


def _dispatch_index_jobs_safely() -> None:
    # 目的：在 API 進程安全觸發派工。
    # 為什麼：避免子進程鏈式再派工造成進程堆疊與記憶體無法回收。
    try:
        _dispatch_index_jobs_once()
    except Exception as dispatch_error:
        logger.warning('rag.index_dispatch_failed', error=str(dispatch_error))


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
        raise not_found_error('Agent', agent_id) from None

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    # Manually parse form to handle both UploadFile and empty-filename form field cases
    form = await request.form()
    uploaded = form.get('file')
    file_size_bytes = 0
    if uploaded is not None and hasattr(uploaded, 'filename') and getattr(uploaded, 'filename', None):
        filename = _safe_filename(uploaded.filename)
        content_type = getattr(uploaded, 'content_type', 'application/octet-stream')
    else:
        filename = 'uploaded'
        content_type = 'application/octet-stream'

    save_dir = UPLOAD_ROOT / str(agent_id)
    save_dir.mkdir(parents=True, exist_ok=True)
    file_key = f'{uuid.uuid4()}_{filename}'
    saved_path = save_dir / file_key
    if uploaded is not None and hasattr(uploaded, 'read'):
        try:
            file_size_bytes = await _persist_upload_to_path(uploaded_file_obj=uploaded, save_path=saved_path)
        except Exception:
            file_size_bytes = 0
            with open(saved_path, 'wb') as f:
                f.write(b'')
    else:
        with open(saved_path, 'wb') as f:
            f.write(b'')

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

    file_content = b''
    if file_size_bytes <= MAX_SYNC_INDEX_FILE_BYTES and file_size_bytes > 0:
        try:
            file_content = saved_path.read_bytes()
        except Exception:
            file_content = b''

    extracted_text = ''
    extract_error = None
    chunks_with_meta: list[dict[str, int | str | None]] = []
    is_embedding_ready, embedding_error = _ensure_embedding_ready_for_indexing()
    is_qdrant_ready, qdrant_error = _ensure_qdrant_ready_for_indexing()
    if file_size_bytes > MAX_SYNC_INDEX_FILE_BYTES:
        extract_error = f'file_too_large_for_sync_index:max={MAX_SYNC_INDEX_FILE_BYTES}:actual={file_size_bytes}'
    elif file_size_bytes <= 0:
        extract_error = 'empty_file'
    elif not is_embedding_ready:
        extract_error = str(embedding_error or 'embedding_provider_unavailable')
    elif not is_qdrant_ready:
        extract_error = str(qdrant_error or 'vector_store_unavailable')
    else:
        extracted_text, extract_error = _extract_text_for_indexing(file_content, content_type, filename, str(saved_path))
        chunks_with_meta = _build_chunks_for_indexing(text=extracted_text, filename=filename, content=file_content)
    indexed = False
    status = 'uploaded'
    last_error = None
    if file_size_bytes > MAX_SYNC_INDEX_FILE_BYTES:
        progress_file_path = str(_dataset_progress_dir_by_dataset_id(str(agent_id)) / f'{file_key}.json')
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'queued',
                'stage': 'queued',
                'progress': 0,
                'file_key': file_key,
                'filename': filename,
            },
        )
        try:
            _enqueue_index_job(
                job_type='agent_document',
                kwargs={
                    'agent_id': str(agent_id),
                    'document_id': str(doc.id),
                    'filename': filename,
                    'file_key': file_key,
                    'file_path': str(saved_path),
                    'content_type': content_type,
                    'progress_file_path': progress_file_path,
                },
            )
            _dispatch_index_jobs_safely()
            latest_progress_payload = _read_progress_file(progress_file_path=progress_file_path)
            latest_status = str(latest_progress_payload.get('status') or '').strip().lower()
            status = latest_status if latest_status in {'queued', 'indexing'} else 'queued'
            last_error = None
        except Exception as error:
            status = 'failed'
            last_error = f'background_index_queue_failed:{error.__class__.__name__}'
    elif not extracted_text.strip() or not chunks_with_meta:
        status = 'uploaded'
        last_error = extract_error or 'unsupported_or_empty_content'
    else:
        indexed, index_error = _index_document_chunks(
            agent_id=str(agent_id),
            document_id=str(doc.id),
            filename=filename,
            file_key=file_key,
            chunks_with_meta=chunks_with_meta,
        )
        if indexed:
            status = 'ready'
            last_error = None
        else:
            status = 'failed'
            last_error = index_error or 'index_upsert_failed'

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
        'size_bytes': int(file_size_bytes or 0),
        'message': (
            '文件已索引完成'
            if status == 'ready'
            else ('文件已上傳，已排入背景索引佇列' if status in {'queued', 'indexing'} else (f'文件已上傳，但無法索引：{last_error}' if last_error else '文件已上傳'))
        ),
        'progress': (0 if status in {'queued', 'indexing'} else 100),
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
        raise not_found_error('Agent', agent_id) from None

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise not_found_error('Agent', agent_id)

    _dispatch_index_jobs_safely()

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
        raise not_found_error('Document', doc_id) from None

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

    try:
        collection_name = _agent_collection_name(str(agent_id))
        qdrant_service.delete_by_payload_match(
            collection_name=collection_name,
            match_fields={
                'document_id': str(doc.id),
                'agent_id': str(agent_id),
            },
        )
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
        raise not_found_error('RagDataset', dataset_id) from None

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid).first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    # 權限檢查：公有資料集需要 admin，私有資料集需要 read_agent 權限
    if row.scope == 'global':
        if current_user.role != 'admin':
            raise forbidden_error()
    elif row.scope == 'agent_private':
        if not check_permission(current_user, 'update_agent'):
            raise forbidden_error()
    else:
        raise forbidden_error()

    form = await request.form()
    uploaded = form.get('file')
    file_size_bytes = 0
    if uploaded is not None and hasattr(uploaded, 'filename') and getattr(uploaded, 'filename', None):
        filename = _safe_filename(uploaded.filename)
        content_type = getattr(uploaded, 'content_type', 'application/octet-stream')
    else:
        filename = 'uploaded'
        content_type = 'application/octet-stream'

    save_dir = _dataset_upload_dir(row)
    save_dir.mkdir(parents=True, exist_ok=True)
    file_key = f'{uuid.uuid4()}_{filename}'
    saved_path = save_dir / file_key
    if uploaded is not None and hasattr(uploaded, 'read'):
        try:
            file_size_bytes = await _persist_upload_to_path(uploaded_file_obj=uploaded, save_path=saved_path)
        except Exception:
            file_size_bytes = 0
            with open(saved_path, 'wb') as f:
                f.write(b'')
    else:
        with open(saved_path, 'wb') as f:
            f.write(b'')

    file_content = b''
    if file_size_bytes <= MAX_SYNC_INDEX_FILE_BYTES and file_size_bytes > 0:
        try:
            file_content = saved_path.read_bytes()
        except Exception:
            file_content = b''

    extracted_text = ''
    extract_error = None
    chunks_with_meta: list[dict[str, int | str | None]] = []
    is_embedding_ready, embedding_error = _ensure_embedding_ready_for_indexing()
    is_qdrant_ready, qdrant_error = _ensure_qdrant_ready_for_indexing()
    if file_size_bytes > MAX_SYNC_INDEX_FILE_BYTES:
        extract_error = f'file_too_large_for_sync_index:max={MAX_SYNC_INDEX_FILE_BYTES}:actual={file_size_bytes}'
    elif file_size_bytes <= 0:
        extract_error = 'empty_file'
    elif not is_embedding_ready:
        extract_error = str(embedding_error or 'embedding_provider_unavailable')
    elif not is_qdrant_ready:
        extract_error = str(qdrant_error or 'vector_store_unavailable')
    else:
        extracted_text, extract_error = _extract_text_for_indexing(file_content, content_type, filename, str(saved_path))
        chunks_with_meta = _build_chunks_for_indexing(text=extracted_text, filename=filename, content=file_content)
    collection = _dataset_collection_name(row)
    progress_file_path = str(_dataset_progress_file_path(dataset_row=row, file_key=file_key))
    indexed = False
    status = 'uploaded'
    if file_size_bytes > MAX_SYNC_INDEX_FILE_BYTES:
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'queued',
                'stage': 'queued',
                'progress': 0,
                'file_key': file_key,
                'filename': filename,
            },
        )
        try:
            _enqueue_index_job(
                job_type='dataset_document',
                kwargs={
                    'collection_name': collection,
                    'dataset_id': str(row.id),
                    'filename': filename,
                    'file_key': file_key,
                    'file_path': str(saved_path),
                    'content_type': content_type,
                    'progress_file_path': progress_file_path,
                },
            )
            _dispatch_index_jobs_safely()
            latest_progress_payload = _read_progress_file(progress_file_path=progress_file_path)
            latest_status = str(latest_progress_payload.get('status') or '').strip().lower()
            status = latest_status if latest_status in {'queued', 'indexing'} else 'queued'
            extract_error = None
        except Exception as error:
            status = 'failed'
            extract_error = f'background_index_queue_failed:{error.__class__.__name__}'
    elif extracted_text.strip() and chunks_with_meta:
        indexed, index_error = _index_dataset_chunks(
            collection_name=collection,
            dataset_id=str(row.id),
            filename=filename,
            file_key=file_key,
            chunks_with_meta=chunks_with_meta,
        )
        status = 'ready' if indexed else 'failed'
        if not indexed and not extract_error:
            extract_error = str(index_error or 'index_failed')
    else:
        status = 'uploaded'

    if status == 'ready':
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'ready',
                'stage': 'done',
                'progress': 100,
                'file_key': file_key,
                'filename': filename,
                'indexed_at': datetime.now().isoformat(),
                'last_error': None,
            },
        )
    elif status == 'failed':
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'failed',
                'stage': 'failed',
                'progress': 100,
                'file_key': file_key,
                'filename': filename,
                'last_error': str(extract_error or 'index_failed'),
            },
        )
    elif status == 'uploaded':
        _write_progress_file(
            progress_file_path=progress_file_path,
            payload={
                'status': 'uploaded',
                'stage': 'uploaded',
                'progress': 0,
                'file_key': file_key,
                'filename': filename,
                'last_error': str(extract_error or ''),
            },
        )

    return {
        'ok': True,
        'dataset_id': str(row.id),
        'collection': collection,
        'filename': filename,
        'file_key': file_key,
        'file_path': str(saved_path),
        'size_bytes': int(file_size_bytes or 0),
        'status': status,
        'indexed': bool(indexed),
        'message': (
            '文件已索引完成'
            if status == 'ready'
            else ('文件已上傳，已排入背景索引佇列' if status in {'queued', 'indexing'} else (f'文件已上傳，但無法索引：{extract_error or "unsupported_or_empty_content"}' if not indexed else '文件已上傳'))
        ),
        'last_error': (
            None
            if indexed
            else (extract_error if status in {'queued', 'indexing'} else (extract_error or 'unsupported_or_empty_content'))
        ),
        'progress': (0 if status in {'queued', 'indexing'} else 100),
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
            RagDataset.sensitivity == 'normal',
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

    default_global_rows = db.query(RagDataset).filter(
        RagDataset.scope == 'global',
        RagDataset.enabled == True,  # noqa: E712
        RagDataset.sensitivity == 'normal',
    ).all()

    if not dataset_ids:
        sorted_default_rows = sorted(default_global_rows, key=lambda x: x.created_at or datetime.min, reverse=True)
        return {'datasets': [_dataset_to_dict(r) for r in sorted_default_rows]}

    rows = db.query(RagDataset).filter(
        RagDataset.id.in_(dataset_ids),
        RagDataset.enabled == True,  # noqa: E712
    ).all()

    out = []
    out_ids: set[str] = set()
    for row in rows:
        if row.scope == 'global':
            out.append(row)
            out_ids.add(str(row.id))
            continue
        if row.scope == 'agent_private' and str(getattr(row, 'agent_id', '') or '') == str(agent.id):
            out.append(row)
            out_ids.add(str(row.id))

    for default_row in default_global_rows:
        default_row_id = str(default_row.id)
        if default_row_id in out_ids:
            continue
        out.append(default_row)
        out_ids.add(default_row_id)

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
        raise not_found_error('RagDataset', dataset_id) from None

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
        raise not_found_error('RagDataset', dataset_id) from None

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

    _dispatch_index_jobs_safely()

    upload_dir = _dataset_upload_dir(row)
    documents = []
    progress_dir = _dataset_progress_dir(row)
    if upload_dir.is_dir():
        for fname in os.listdir(upload_dir):
            fpath = upload_dir / fname
            if fpath.is_file():
                stat = fpath.stat()
                # 去掉 UUID 前綴顯示原始檔名（格式：{uuid}_{原始檔名}）
                display_name = _display_name_from_stored_key(fname)
                progress_payload = _read_progress_file(progress_file_path=str(progress_dir / f'{fname}.json'))
                progress_file_path = str(progress_dir / f'{fname}.json')
                progress_value_raw = progress_payload.get('progress')
                progress_value = int(progress_value_raw) if isinstance(progress_value_raw, int | float) else None
                normalized_status = str(progress_payload.get('status') or '').strip().lower()
                if normalized_status not in {'uploaded', 'queued', 'indexing', 'ready', 'failed'}:
                    normalized_status = 'uploaded'
                if normalized_status == 'indexing' and _is_progress_stale(progress_payload=progress_payload):
                    stage_name = str(progress_payload.get('stage') or '').strip().lower()
                    process_pid_raw = progress_payload.get('process_pid')
                    process_pid = int(process_pid_raw) if isinstance(process_pid_raw, int | float) else None
                    process_alive = _is_process_alive(process_pid)
                    should_fail_for_startup_stall = process_alive is None and stage_name in {'starting', 'embedding_preflight', ''}
                    should_fail_for_docling_stall = process_alive is None and stage_name in {'docling_wait', 'docling_extracting'}
                    should_fail_for_terminated_process = process_alive is False
                    if should_fail_for_startup_stall or should_fail_for_docling_stall or should_fail_for_terminated_process:
                        stalled_error = str(progress_payload.get('last_error') or '').strip()
                        if not stalled_error:
                            stalled_error = (
                                f'index_process_terminated:pid={process_pid}'
                                if should_fail_for_terminated_process and isinstance(process_pid, int) and process_pid > 0
                                else ('docling_extracting_stalled_or_process_terminated' if should_fail_for_docling_stall else 'indexing_stalled_or_process_terminated')
                            )
                        normalized_status = 'failed'
                        progress_payload = {
                            **dict(progress_payload or {}),
                            'status': 'failed',
                            'stage': 'failed',
                            'progress': 100,
                            'last_error': stalled_error,
                            'file_key': fname,
                            'filename': display_name,
                        }
                        _write_progress_file(progress_file_path=progress_file_path, payload=progress_payload)
                        progress_value = 100
                documents.append({
                    'file_key': fname,
                    'filename': display_name,
                    'file_path': str(fpath),
                    'size_bytes': stat.st_size,
                    'uploaded_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    'status': normalized_status,
                    'progress': max(0, min(100, int(progress_value))) if isinstance(progress_value, int) else (100 if normalized_status == 'ready' else (0 if normalized_status in {'queued', 'indexing'} else None)),
                    'stage': str(progress_payload.get('stage') or '').strip(),
                    'last_error': str(progress_payload.get('last_error') or '').strip() or None,
                    'indexed_at': str(progress_payload.get('indexed_at') or '').strip() or None,
                })

    # 依上傳時間倒序
    documents.sort(key=lambda d: d['uploaded_at'], reverse=True)

    return {
        'ok': True,
        'dataset_id': str(row.id),
        'documents': documents,
        'total_count': len(documents),
    }


@router.get('/rag/datasets/{dataset_id}/documents/{file_key}/open')
async def open_dataset_document(
    dataset_id: str,
    file_key: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
):
    # 目的：提供聊天引用來源的可點擊文件連結。
    # 為什麼：讓使用者可直接開啟檔案驗證 RAG 回覆內容來源。
    _row, target_path = _resolve_dataset_document_context(
        dataset_id=dataset_id,
        file_key=file_key,
        request=request,
        credentials=credentials,
        db=db,
    )

    media_type, _encoding = mimetypes.guess_type(str(target_path))
    return FileResponse(
        path=str(target_path),
        media_type=media_type or 'application/octet-stream',
    )


@router.get('/rag/datasets/{dataset_id}/documents/{file_key}/open-preview')
async def open_dataset_document_preview(
    dataset_id: str,
    file_key: str,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
):
    # 目的：提供 docx 等不易 inline 預覽的文件網頁版瀏覽。
    # 為什麼：瀏覽器通常只會下載 docx，需轉成 HTML 才能直接閱讀來源內容。
    _row, target_path = _resolve_dataset_document_context(
        dataset_id=dataset_id,
        file_key=file_key,
        request=request,
        credentials=credentials,
        db=db,
    )

    normalized_suffix = str(target_path.suffix or '').strip().lower()
    if normalized_suffix == '.pdf':
        media_type, _encoding = mimetypes.guess_type(str(target_path))
        return FileResponse(path=str(target_path), media_type=media_type or 'application/pdf')

    markdown_text, markdown_error = convert_document_to_markdown(str(target_path))
    if markdown_error:
        raise not_found_error('Document', f'preview_unavailable:{markdown_error}')

    safe_title = html.escape(_display_name_from_stored_key(str(target_path.name)))
    safe_content = html.escape(markdown_text).replace('\n', '<br>')
    html_content = (
        '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
        f'<title>{safe_title}</title>'
        '<style>body{font-family: "Noto Sans TC", "PingFang TC", sans-serif;max-width:960px;margin:24px auto;padding:0 16px;line-height:1.7;color:#0f172a;background:#f8fafc;}'
        'h1{font-size:20px;margin-bottom:12px;} .doc{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:18px 20px;white-space:normal;word-break:break-word;} '
        '.tip{color:#64748b;font-size:13px;margin-bottom:10px;}</style></head><body>'
        f'<h1>{safe_title}</h1><div class="tip">以下內容由文件轉換為可閱讀預覽格式。</div><div class="doc">{safe_content}</div></body></html>'
    )
    return HTMLResponse(content=html_content)


@router.delete('/rag/datasets/{dataset_id}/documents/{file_key}')
async def delete_dataset_document(
    dataset_id: str,
    file_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 目的：刪除單次上傳的資料集文件（實體檔與向量）。
    # 為什麼：上傳選錯檔時必須可完全回滾，避免殘留向量污染查詢結果。
    try:
        dataset_uuid = uuid.UUID(str(dataset_id))
    except Exception:
        raise not_found_error('RagDataset', dataset_id) from None

    normalized_file_key = _parse_dataset_file_key(file_key)
    if not normalized_file_key:
        raise not_found_error('Document', file_key)

    row = db.query(RagDataset).filter(RagDataset.id == dataset_uuid).first()
    if not row:
        raise not_found_error('RagDataset', dataset_id)

    if row.scope == 'global':
        if current_user.role != 'admin':
            raise forbidden_error()
    elif row.scope == 'agent_private':
        if not check_permission(current_user, 'read_agent'):
            raise forbidden_error()
    else:
        raise forbidden_error()

    upload_dir = _dataset_upload_dir(row)
    target_path = upload_dir / normalized_file_key
    if not target_path.exists() or not target_path.is_file():
        raise not_found_error('Document', normalized_file_key)

    file_deleted = False
    try:
        os.remove(target_path)
        file_deleted = True
    except Exception:
        file_deleted = False

    try:
        progress_file = _dataset_progress_file_path(dataset_row=row, file_key=normalized_file_key)
        if progress_file.exists() and progress_file.is_file():
            os.remove(progress_file)
    except Exception:
        pass

    collection_name = _dataset_collection_name(row)
    vector_deleted = qdrant_service.delete_by_payload_match(
        collection_name=collection_name,
        match_fields={
            'dataset_id': str(row.id),
            'file_key': normalized_file_key,
        },
    )

    if not vector_deleted:
        fallback_filename = _display_name_from_stored_key(normalized_file_key)
        qdrant_service.delete_by_payload_match(
            collection_name=collection_name,
            match_fields={
                'dataset_id': str(row.id),
                'filename': fallback_filename,
            },
        )

    return {
        'ok': True,
        'dataset_id': str(row.id),
        'file_key': normalized_file_key,
        'file_deleted': bool(file_deleted),
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
