from src.models import Agent
from src.services.embedding_service import embedding_service


_COMPOUND_SIGNALS = [
    '並且', '同時', '另外', '然後', '接著', '以及', '也要', '還要', '順便',
    'and then', 'also', 'additionally', 'as well',
]


def _classify_routing(*, message: str, workers: list[Agent]) -> str:
    """目的：三層策略判斷單代理 vs 多代理路徑。
    為什麼：優先用零成本快路徑，僅必要時才呼叫 LLM，降低延遲與費用。
    回傳 'single' 或 'multi'。
    """
    # 第一層：快速排除（零成本）
    if len(message.strip()) < 15:
        return 'single'
    if len(workers) < 2:
        return 'single'
    lower_msg = message.lower()
    has_compound = any(sig in lower_msg for sig in _COMPOUND_SIGNALS)
    has_mention = any(f'@{str(w.name or "").lower()}' in lower_msg for w in workers)

    # 直接點名 2 個以上 worker：不需要 embedding，直接走多代理
    named_workers = [w for w in workers if str(w.name or '').strip() and str(w.name or '').strip() in message]
    if len(named_workers) >= 2:
        return 'multi'

    if not has_compound and not has_mention:
        return 'single'

    # 第二層：Embedding 能力距離（無 LLM 費用）
    try:
        msg_vec = embedding_service.embed_one(message)
        if msg_vec:
            scores: list[tuple[float, Agent]] = []
            for w in workers:
                profile = f"{str(w.name or '').strip()}\n{str(w.description or '').strip()}"
                wvec = embedding_service.embed_one(profile)
                scores.append((_cosine_similarity(msg_vec, wvec), w))
            scores.sort(key=lambda x: x[0], reverse=True)
            if len(scores) >= 2:
                top_gap = scores[0][0] - scores[1][0]
                if top_gap > 0.2:
                    return 'single'  # 某個 Worker 明顯更適合
    except Exception:
        pass

    return 'multi'

def _cosine_similarity(v1: list[float], v2: list[float]) -> float:
    if not v1 or not v2 or len(v1) != len(v2):
        return -1.0
    dot = sum((a * b) for a, b in zip(v1, v2))
    n1 = sum((a * a) for a in v1) ** 0.5
    n2 = sum((b * b) for b in v2) ** 0.5
    if n1 <= 0 or n2 <= 0:
        return -1.0
    return float(dot / (n1 * n2))

