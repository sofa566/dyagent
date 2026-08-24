from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointIdsList,
    PointStruct,
    VectorParams,
)
from typing import Any

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger(__name__)


def _is_collection_not_found_error(error: Exception) -> bool:
    message_text = str(error or '').lower()
    return (
        '404' in message_text
        and 'not found' in message_text
        and 'collection' in message_text
        and "doesn't exist" in message_text
    )


class QdrantService:
    def __init__(self):
        self.client: QdrantClient | None = None

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
        if not self.client:
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

    def upsert_vectors(
        self,
        collection_name: str,
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        ids: list[str] | None = None,
    ):
        if not self.client:
            return False

        try:
            points = [
                PointStruct(
                    id=id_ or str(i),
                    vector=vector,
                    payload=payload,
                )
                for i, (vector, payload, id_) in enumerate(zip(vectors, payloads, ids or [None] * len(vectors)))
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
        if not self.client:
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

    def get_collection_vector_size(self, collection_name: str) -> int | None:
        if not self.client:
            return None

        try:
            collection_info = self.client.get_collection(collection_name=collection_name)
            vectors_config = getattr(getattr(collection_info, 'config', None), 'params', None)
            vectors_value = getattr(vectors_config, 'vectors', None)
            if isinstance(vectors_value, VectorParams):
                return int(vectors_value.size)
            if isinstance(vectors_value, dict) and vectors_value:
                first_value = next(iter(vectors_value.values()))
                if isinstance(first_value, VectorParams):
                    return int(first_value.size)
                size_value = getattr(first_value, 'size', None)
                if isinstance(size_value, int):
                    return int(size_value)
            return None
        except Exception as error:
            if _is_collection_not_found_error(error):
                logger.info('collection_vector_size_collection_missing', collection=collection_name)
                return None
            logger.warning('collection_vector_size_failed', collection=collection_name, error=str(error))
            return None

    def delete_by_payload_match(self, *, collection_name: str, match_fields: dict[str, Any]) -> bool:
        if not self.client:
            return False

        normalized_fields = {
            str(key): value
            for key, value in dict(match_fields or {}).items()
            if str(key or '').strip()
        }
        if not normalized_fields:
            return False

        conditions = [
            FieldCondition(key=field_name, match=MatchValue(value=field_value))
            for field_name, field_value in normalized_fields.items()
        ]
        query_filter = Filter(must=conditions)

        try:
            point_ids: list[Any] = []
            next_offset: Any | None = None
            while True:
                points, next_offset = self.client.scroll(
                    collection_name=collection_name,
                    scroll_filter=query_filter,
                    with_payload=False,
                    with_vectors=False,
                    limit=256,
                    offset=next_offset,
                )
                for point in points:
                    point_id = getattr(point, 'id', None)
                    if point_id is not None:
                        point_ids.append(point_id)
                if next_offset is None:
                    break

            if not point_ids:
                return True

            self.client.delete(
                collection_name=collection_name,
                points_selector=PointIdsList(points=point_ids),
            )
            logger.info('vectors_deleted_by_payload_match', collection=collection_name, count=len(point_ids))
            return True
        except Exception as error:
            logger.error(
                'vectors_delete_by_payload_match_failed',
                collection=collection_name,
                error=str(error),
            )
            return False


qdrant_service = QdrantService()
