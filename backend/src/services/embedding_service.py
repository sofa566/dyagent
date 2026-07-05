from __future__ import annotations

from typing import List
import httpx

from src.core.config import settings
from src.core.logging import get_logger
from src.services.rag_vectorizer import text_to_vector


logger = get_logger(__name__)


class EmbeddingService:
    # 目的：統一向量化入口，支援雲端/地端/退回策略。
    # 為什麼：避免 RAG 流程直接耦合單一模型，便於切換部署模式。
    def __init__(self):
        self._st_model = None

    def embed_texts(self, texts: List[str]) -> List[list[float]]:
        provider = str(settings.EMBEDDING_PROVIDER or 'deterministic').strip().lower()
        clean_texts = [str(t or '') for t in (texts or [])]
        if not clean_texts:
            return []

        if provider == 'sentence_transformers':
            vectors = self._embed_by_sentence_transformers(clean_texts)
            if vectors:
                return vectors
        elif provider == 'ollama':
            vectors = self._embed_by_ollama(clean_texts)
            if vectors:
                return vectors
        elif provider == 'vllm':
            vectors = self._embed_by_vllm(clean_texts)
            if vectors:
                return vectors

        return [text_to_vector(text) for text in clean_texts]

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed_texts([text])
        return vectors[0] if vectors else text_to_vector(str(text or ''))

    def _embed_by_sentence_transformers(self, texts: List[str]) -> List[list[float]]:
        try:
            if self._st_model is None:
                from sentence_transformers import SentenceTransformer

                self._st_model = SentenceTransformer(settings.EMBEDDING_MODEL_NAME)
            embeddings = self._st_model.encode(texts, normalize_embeddings=True)
            return [list(map(float, vec)) for vec in embeddings]
        except Exception as e:
            logger.warning('embedding.sentence_transformers.failed', error=str(e))
            return []

    def _embed_by_ollama(self, texts: List[str]) -> List[list[float]]:
        vectors: List[list[float]] = []
        base_url = str(settings.EMBEDDING_OLLAMA_BASE_URL or '').rstrip('/')
        model = str(settings.EMBEDDING_MODEL_NAME or '').strip()
        if not base_url or not model:
            return []

        try:
            with httpx.Client(timeout=20.0) as client:
                for text in texts:
                    response = client.post(
                        f'{base_url}/api/embeddings',
                        json={'model': model, 'prompt': text},
                    )
                    if response.status_code != 200:
                        logger.warning('embedding.ollama.http_error', status_code=response.status_code)
                        return []
                    data = response.json() if response.content else {}
                    vector = data.get('embedding') if isinstance(data, dict) else None
                    if not isinstance(vector, list) or not vector:
                        logger.warning('embedding.ollama.invalid_payload')
                        return []
                    vectors.append([float(x) for x in vector])
        except Exception as e:
            logger.warning('embedding.ollama.failed', error=str(e))
            return []
        return vectors

    def _embed_by_vllm(self, texts: List[str]) -> List[list[float]]:
        base_url = str(settings.EMBEDDING_VLLM_BASE_URL or '').rstrip('/')
        model = str(settings.EMBEDDING_MODEL_NAME or '').strip()
        if not base_url or not model:
            return []

        endpoint = f'{base_url}/embeddings' if base_url.endswith('/v1') else f'{base_url}/v1/embeddings'
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    endpoint,
                    json={'model': model, 'input': texts},
                )
                if response.status_code != 200:
                    logger.warning('embedding.vllm.http_error', status_code=response.status_code)
                    return []
                data = response.json() if response.content else {}
                rows = data.get('data') if isinstance(data, dict) else None
                if not isinstance(rows, list) or not rows:
                    logger.warning('embedding.vllm.invalid_payload')
                    return []

                vectors: List[list[float]] = []
                for row in rows:
                    vector = row.get('embedding') if isinstance(row, dict) else None
                    if not isinstance(vector, list) or not vector:
                        logger.warning('embedding.vllm.invalid_vector_item')
                        return []
                    vectors.append([float(x) for x in vector])
                return vectors
        except Exception as e:
            logger.warning('embedding.vllm.failed', error=str(e))
            return []


embedding_service = EmbeddingService()
