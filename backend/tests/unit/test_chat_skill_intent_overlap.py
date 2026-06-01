from __future__ import annotations

from types import SimpleNamespace

from src.api.routes.chat import _find_best_skill_name
from src.api.routes.chat import _has_meaningful_skill_overlap
from src.api.routes import chat as chat_routes


def _make_skill(*, skill_id: str, name: str, description: str):
    return SimpleNamespace(
        id=skill_id,
        name=name,
        description=description,
        prompt_template='',
    )


def test_has_meaningful_skill_overlap_rejects_only_taiwan_overlap() -> None:
    news_skill = _make_skill(
        skill_id='s1',
        name='taiwan-news-rss',
        description='查詢台灣新聞與最新新聞摘要',
    )

    matched = _has_meaningful_skill_overlap(
        skill_row=news_skill,
        normalized_message='台灣的十大景點有哪些？',
    )

    assert matched is False


def test_has_meaningful_skill_overlap_accepts_specific_topic_token() -> None:
    news_skill = _make_skill(
        skill_id='s2',
        name='taiwan-news-rss',
        description='查詢台灣新聞與最新新聞摘要',
    )

    matched = _has_meaningful_skill_overlap(
        skill_row=news_skill,
        normalized_message='幫我整理今天的台灣新聞重點',
    )

    assert matched is True


def test_find_best_skill_name_returns_none_when_only_ambiguous_overlap() -> None:
    news_skill = _make_skill(
        skill_id='s3',
        name='taiwan-news-rss',
        description='查詢台灣新聞與最新新聞摘要',
    )

    selected = _find_best_skill_name(
        skill_rows=[news_skill],
        normalized_message='台灣的十大景點有哪些？',
    )

    assert selected is None


def test_has_meaningful_skill_overlap_respects_configurable_ambiguous_tokens(monkeypatch) -> None:
    news_skill = _make_skill(
        skill_id='s4',
        name='taiwan-news-rss',
        description='查詢台灣新聞與最新新聞摘要',
    )

    monkeypatch.setattr(
        chat_routes.settings,
        'SKILL_INTENT_AMBIGUOUS_TOKENS',
        'taiwan,news',
        raising=False,
    )
    chat_routes._load_skill_intent_ambiguous_tokens.cache_clear()

    matched = _has_meaningful_skill_overlap(
        skill_row=news_skill,
        normalized_message='taiwan news',
    )

    assert matched is False

    monkeypatch.setattr(chat_routes.settings, 'SKILL_INTENT_AMBIGUOUS_TOKENS', '', raising=False)
    chat_routes._load_skill_intent_ambiguous_tokens.cache_clear()
