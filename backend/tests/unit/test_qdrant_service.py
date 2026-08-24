from __future__ import annotations

import src.services.qdrant_service as qdrant_module


class _MissingCollectionClient:
    def get_collection(self, *, collection_name: str):
        raise Exception(
            "Unexpected Response: 404 (Not Found) Raw response content: Not found: Collection `test` doesn't exist!"
        )


class _BrokenClient:
    def get_collection(self, *, collection_name: str):
        raise Exception('socket timeout')


def test_get_collection_vector_size_collection_missing_logs_info_only(monkeypatch) -> None:
    service = qdrant_module.QdrantService()
    service.client = _MissingCollectionClient()
    captured = {'warning': 0, 'info': 0}

    def _capture_warning(*_args, **_kwargs):
        captured['warning'] += 1

    def _capture_info(*_args, **_kwargs):
        captured['info'] += 1

    monkeypatch.setattr(qdrant_module.logger, 'warning', _capture_warning)
    monkeypatch.setattr(qdrant_module.logger, 'info', _capture_info)

    result = service.get_collection_vector_size(collection_name='rag_dataset_demo')

    assert result is None
    assert captured['warning'] == 0
    assert captured['info'] == 1


def test_get_collection_vector_size_unexpected_error_keeps_warning(monkeypatch) -> None:
    service = qdrant_module.QdrantService()
    service.client = _BrokenClient()
    captured = {'warning': 0}

    def _capture_warning(*_args, **_kwargs):
        captured['warning'] += 1

    monkeypatch.setattr(qdrant_module.logger, 'warning', _capture_warning)

    result = service.get_collection_vector_size(collection_name='rag_dataset_demo')

    assert result is None
    assert captured['warning'] == 1
