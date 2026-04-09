from __future__ import annotations

import json
import os
import sys
from typing import Any

from src.skills.taiwan_finance_news import run as run_taiwan_finance_news
from src.skills.taiwan_news import run as run_taiwan_news
from src.skills.demo_fx import convert as run_demo_fx_convert


def _load_skill_input() -> dict[str, Any]:
    raw = str(os.environ.get('SKILL_INPUT') or '').strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return {}
    except Exception:
        return {}


def _dispatch(skill_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    # 目的：集中 command runner 的技能分派入口。
    # 為什麼：讓 command 欄位固定使用同一條命令，避免每個技能都要寫一段長 inline Python。
    normalized_name = str(skill_name or '').strip().lower()
    if normalized_name == 'taiwan-news-rss':
        return run_taiwan_news(payload)
    if normalized_name == 'taiwan-finance-news-rss':
        return run_taiwan_finance_news(payload)
    if normalized_name == '價格換算':
        return run_demo_fx_convert(payload)
    return {
        'ok': False,
        'error': f'unsupported_skill: {normalized_name}',
        'text': f'不支援的技能名稱：{normalized_name}',
    }


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if not args:
        print(json.dumps({'ok': False, 'error': 'missing_skill_name'}, ensure_ascii=False))
        return 2
    payload = _load_skill_input()
    result = _dispatch(args[0], payload)
    print(json.dumps(result, ensure_ascii=False))
    if bool(result.get('ok')):
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
