from __future__ import annotations

import sys
import types

from src.tools.document_convert import convert_document, convert_document_to_markdown


def test_convert_document_returns_not_found_for_missing_file():
    result = convert_document('/tmp/not-exists-dyagent.md')
    assert result.get('ok') is False
    assert str(result.get('error') or '').startswith('file_not_found:')


def test_convert_document_to_markdown_with_mocked_docling(tmp_path, monkeypatch):
    # 目的：驗證文件轉換流程可在 docling 可用時輸出 markdown。
    # 為什麼：避免依賴真實 docling 執行成本，改以 mock 保護轉換介面契約。
    source_file = tmp_path / 'sample.txt'
    source_file.write_text('sample content', encoding='utf-8')

    fake_docling_module = types.ModuleType('docling')
    fake_docling_document_converter = types.ModuleType('docling.document_converter')

    class FakeDocument:
        def export_to_markdown(self):
            return '# converted markdown'

        def export_to_json(self):
            return {'ok': True}

    class FakeResult:
        document = FakeDocument()

    class FakeDocumentConverter:
        def convert(self, _path: str):
            return FakeResult()

    fake_docling_document_converter.DocumentConverter = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, 'docling', fake_docling_module)
    monkeypatch.setitem(sys.modules, 'docling.document_converter', fake_docling_document_converter)

    result = convert_document(source_file)
    assert result.get('ok') is True
    assert result.get('markdown') == '# converted markdown'

    markdown, error = convert_document_to_markdown(source_file)
    assert markdown == '# converted markdown'
    assert error is None
