from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
from typing import Any

from src.core.config import settings
from src.core.logging import get_logger

logger = get_logger(__name__)


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


qdrant_service = QdrantService()
