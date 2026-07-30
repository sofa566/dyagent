from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger(__name__)


class QdrantService:
    def __init__(self):
        self.client: QdrantClient | None = None

    def _ensure_client(self) -> bool:
        if self.client is not None:
            return True
        self.connect()
        return self.client is not None

    def connect(self):
        try:
            self.client = QdrantClient(url=settings.QDRANT_URL)
            logger.info('qdrant_connected', url=settings.QDRANT_URL)
        except Exception as e:
            logger.error('qdrant_connection_failed', error=str(e))
            self.client = None

    def disconnect(self):
        logger.info('qdrant_disconnected')

    def create_collection(self, collection_name: str, vector_size: int = 1536):
        if not self._ensure_client():
            return False

        try:
            collections = self.client.get_collections().collections
            if any(c.name == collection_name for c in collections):
                logger.info('collection_already_exists', collection=collection_name)
                return True

            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            logger.info('collection_created', collection=collection_name)
            return True
        except Exception as e:
            logger.error('collection_creation_failed', error=str(e))
            return False

    def get_collection_vector_size(self, collection_name: str) -> int | None:
        # 目的：取得既有 collection 的向量維度。
        # 為什麼：embedding 模型更換後若維度不一致，需在 upsert 前提早阻擋並回報明確錯誤。
        if not self._ensure_client():
            return None
        try:
            info = self.client.get_collection(collection_name=collection_name)
            params = getattr(getattr(info, 'config', None), 'params', None)
            vectors = getattr(params, 'vectors', None)
            if vectors is None:
                return None

            direct_size = getattr(vectors, 'size', None)
            if isinstance(direct_size, int) and direct_size > 0:
                return direct_size

            if isinstance(vectors, dict):
                for value in vectors.values():
                    size = getattr(value, 'size', None)
                    if isinstance(size, int) and size > 0:
                        return size
            return None
        except Exception as e:
            logger.warning('collection_get_vector_size_failed', collection=collection_name, error=str(e))
            return None

    def upsert_vectors(
        self,
        collection_name: str,
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        ids: list[str] | None = None,
    ):
        if not self._ensure_client():
            return False

        try:
            points = [
                PointStruct(
                    id=id_ or str(i),
                    vector=vector,
                    payload=payload,
                )
                for i, (vector, payload, id_) in enumerate(zip(vectors, payloads, ids or [None] * len(vectors), strict=False))
            ]
            self.client.upsert(collection_name=collection_name, points=points)
            logger.info('vectors_upserted', count=len(vectors))
            return True
        except Exception as e:
            logger.error('vector_upsert_failed', error=str(e))
            return False

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        limit: int = 5,
        query_filter: dict | None = None,
    ):
        if not self._ensure_client():
            return []

        try:
            results = self.client.search(
                collection_name=collection_name,
                query_vector=query_vector,
                limit=limit,
                query_filter=query_filter,
            )
            return [
                {'id': r.id, 'score': r.score, 'payload': r.payload}
                for r in results
            ]
        except Exception as e:
            logger.error('search_failed', error=str(e))
            return []

    def delete_by_payload_match(
        self,
        *,
        collection_name: str,
        match_fields: dict[str, str | int | float | bool],
    ) -> bool:
        # 目的：依 payload 等值條件刪除向量點位。
        # 為什麼：文件刪除需同步清理向量索引，避免殘留舊內容影響後續檢索。
        if not self._ensure_client():
            return False
        if not isinstance(match_fields, dict) or not match_fields:
            return False

        try:
            conditions = []
            for key, value in match_fields.items():
                if not isinstance(key, str) or not key.strip():
                    continue
                conditions.append(
                    FieldCondition(
                        key=key.strip(),
                        match=MatchValue(value=value),
                    )
                )
            if not conditions:
                return False

            selector = Filter(must=conditions)
            self.client.delete(collection_name=collection_name, points_selector=selector, wait=True)
            logger.info('vectors_deleted_by_payload_match', collection=collection_name, fields=list(match_fields.keys()))
            return True
        except Exception as e:
            logger.error('vector_delete_by_payload_match_failed', error=str(e), collection=collection_name)
            return False


qdrant_service = QdrantService()
