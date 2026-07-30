import hashlib
import math
from collections.abc import Iterator

VECTOR_SIZE = 1536


def iter_chunk_text(text: str, max_chars: int = 800) -> Iterator[str]:
    # 目的：以惰性方式將文件切成可索引片段。
    # 為什麼：超大檔案若一次建立完整 chunk 清單，會造成記憶體峰值過高。
    if not isinstance(text, str) or not text.strip():
        return

    normalized = text.replace('\r\n', '\n').strip()
    blocks = [seg.strip() for seg in normalized.split('\n\n') if seg.strip()]
    if not blocks:
        blocks = [normalized]

    for block in blocks:
        if len(block) <= max_chars:
            yield block
            continue

        start = 0
        while start < len(block):
            end = min(start + max_chars, len(block))
            yield block[start:end]
            start = end


def chunk_text(text: str, max_chars: int = 800) -> list[str]:
    # 目的：將文件切成可索引的小片段。
    # 為什麼：避免單段過長影響檢索品質，並降低向量化負擔。
    if not isinstance(text, str) or not text.strip():
        return []

    return list(iter_chunk_text(text, max_chars=max_chars))


def iter_chunk_text_with_pages(page_texts: list[str], max_chars: int = 800) -> Iterator[dict[str, int | str]]:
    # 目的：依頁面惰性切塊並保留頁碼資訊。
    # 為什麼：大型 PDF 若一次建立整份 chunk 陣列，會放大記憶體壓力。
    if not isinstance(page_texts, list) or not page_texts:
        return

    for page_index, page_text in enumerate(page_texts, start=1):
        normalized_page_text = str(page_text or '').strip()
        if not normalized_page_text:
            continue

        for page_chunk in iter_chunk_text(normalized_page_text, max_chars=max_chars):
            chunk_text_value = str(page_chunk or '').strip()
            if not chunk_text_value:
                continue
            yield {
                'text': chunk_text_value,
                'page_number': page_index,
            }


def chunk_text_with_pages(page_texts: list[str], max_chars: int = 800) -> list[dict[str, int | str]]:
    # 目的：依頁面切塊並保留頁碼資訊。
    # 為什麼：RAG 結果需要顯示來源頁碼，必須在索引階段保留 chunk 對應頁。
    return list(iter_chunk_text_with_pages(page_texts, max_chars=max_chars))


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
