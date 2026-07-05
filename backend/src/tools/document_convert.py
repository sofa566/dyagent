from __future__ import annotations

from pathlib import Path
from typing import Any


def convert_document(file_path: str | Path) -> dict[str, Any]:
    # 目的：將任意支援文件轉為 markdown/json 供工具執行階段注入。
    # 為什麼：聊天附件先二進位落地，只有在需要時才做語義化轉換，避免前端格式耦合。
    normalized_path = Path(file_path)
    if not normalized_path.exists() or not normalized_path.is_file():
        return {
            'ok': False,
            'markdown': '',
            'json': None,
            'error': f'file_not_found:{normalized_path}',
        }

    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception as error:
        return {
            'ok': False,
            'markdown': '',
            'json': None,
            'error': f'docling_import_failed:{error}',
        }

    try:
        converter = DocumentConverter()
        result = converter.convert(str(normalized_path))
        document_obj = getattr(result, 'document', None)
        markdown_output = ''
        json_output: Any = None
        if document_obj is not None and hasattr(document_obj, 'export_to_markdown'):
            markdown_output = str(document_obj.export_to_markdown() or '')
        if document_obj is not None and hasattr(document_obj, 'export_to_json'):
            json_output = document_obj.export_to_json()
        return {
            'ok': True,
            'markdown': markdown_output,
            'json': json_output,
            'error': None,
        }
    except Exception as error:
        return {
            'ok': False,
            'markdown': '',
            'json': None,
            'error': f'docling_convert_failed:{error}',
        }


def convert_document_to_markdown(file_path: str | Path) -> tuple[str, str | None]:
    # 目的：提供聊天注入流程最小介面（markdown + error）。
    # 為什麼：工具呼叫只關心可讀文字，不需每次處理完整 JSON 結構。
    result = convert_document(file_path)
    if not bool(result.get('ok')):
        return '', str(result.get('error') or 'convert_failed')
    markdown = str(result.get('markdown') or '').strip()
    if not markdown:
        return '', 'empty_markdown'
    return markdown, None
