import hashlib
import math
from collections.abc import Iterator


VECTOR_SIZE = 1536


def iter_chunk_text(text: str, max_chars: int = 800) -> Iterator[str]:
    for chunk in chunk_text(text, max_chars=max_chars):
        yield chunk


def iter_chunk_text_with_pages(page_texts: list[str], max_chars: int = 800) -> Iterator[dict[str, int | str]]:
    for page_number, page_text in enumerate(page_texts or [], start=1):
        for chunk in chunk_text(page_text, max_chars=max_chars):
            if chunk:
                yield {'text': chunk, 'page_number': page_number}


def chunk_text(text: str, max_chars: int = 800) -> list[str]:
    # 目的：將文件切成可索引的小片段。
    # 為什麼：避免單段過長影響檢索品質，並降低向量化負擔。
    if not isinstance(text, str) or not text.strip():
        return []

    normalized = text.replace('\r\n', '\n').strip()
    blocks = [seg.strip() for seg in normalized.split('\n\n') if seg.strip()]
    if not blocks:
        blocks = [normalized]

    chunks: list[str] = []
    for block in blocks:
        if len(block) <= max_chars:
            chunks.append(block)
            continue
        start = 0
        while start < len(block):
            end = min(start + max_chars, len(block))
            chunks.append(block[start:end])
            start = end
    return chunks


def text_to_vector(text: str, size: int = VECTOR_SIZE) -> list[float]:
    # 目的：將文字轉為固定維度向量。
    # 為什麼：在無外部 embedding 服務時提供可運作的檢索索引流程。
    if not isinstance(text, str):
        text = ''

    seed = hashlib.sha256(text.encode('utf-8', errors='ignore')).digest()
    values = []
    counter = 0
    while len(values) < size:
        digest = hashlib.sha256(seed + counter.to_bytes(4, byteorder='big')).digest()
        for i in range(0, len(digest), 2):
            if len(values) >= size:
                break
            num = int.from_bytes(digest[i:i+2], byteorder='big', signed=False)
            values.append((num / 65535.0) * 2.0 - 1.0)
        counter += 1

    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0:
        return [0.0] * size
    return [v / norm for v in values]
