from __future__ import annotations

from typing import Any

from src.skills import taiwan_news
from src.utils.taiwan_finance_news_sources import build_taiwan_finance_fetch_prompt


def build_default_prompt(top_n: int = 8) -> str:
    # 目的：提供財經新聞技能的預設提示詞。
    # 為什麼：讓代理者在未自訂指令時，也能沿用一致的輸出格式與財經焦點。
    return build_taiwan_finance_fetch_prompt(top_n=top_n)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    # 目的：執行台灣財經新聞抓取與整理。
    # 為什麼：以獨立技能入口給 agent 直接呼叫，避免每次透過 mode 參數切換增加配置複雜度。
    raw = payload if isinstance(payload, dict) else {}
    delegated_payload = dict(raw)
    delegated_payload['mode'] = 'finance'
    result = taiwan_news.run(delegated_payload)
    if not isinstance(result, dict):
        return {
            'ok': False,
            'error': 'invalid_result_type',
            'text': '財經新聞技能執行失敗。',
        }

    result['skill'] = 'taiwan-finance-news-rss'
    return result
