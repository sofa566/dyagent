from __future__ import annotations

from typing import Any
from xml.etree import ElementTree

import requests

from src.utils.taiwan_news_sources import list_taiwan_news_rss_sources
from src.utils.taiwan_finance_news_sources import (
    list_taiwan_finance_rss_sources,
    filter_finance_items,
)


REQUEST_TIMEOUT_SECONDS = 8
MAX_SOURCES = 5
MAX_ITEMS_PER_SOURCE = 10
MODE_GENERAL = 'general'
MODE_FINANCE = 'finance'


def _extract_rss_items(xml_text: str, top_n: int) -> list[dict[str, str]]:
    # 目的：解析 RSS XML 並擷取標題、連結與時間。
    # 為什麼：統一不同來源 RSS 格式，讓上層流程可用一致資料結構產生摘要。
    root = ElementTree.fromstring(xml_text)
    items: list[dict[str, str]] = []
    for node in root.findall('.//item')[:top_n]:
        title = (node.findtext('title') or '').strip()
        link = (node.findtext('link') or '').strip()
        pub_date = (node.findtext('pubDate') or '').strip()
        summary = (
            node.findtext('description')
            or node.findtext('{http://purl.org/rss/1.0/modules/content/}encoded')
            or ''
        ).strip()
        if not title and not link:
            continue
        items.append(
            {
                'title': title,
                'link': link,
                'published_at': pub_date,
                'summary': summary,
            }
        )
    return items


def _resolve_mode(payload: dict[str, Any]) -> str:
    raw_mode = str(payload.get('mode') or MODE_GENERAL).strip().lower()
    return MODE_FINANCE if raw_mode == MODE_FINANCE else MODE_GENERAL


def _select_sources(*, mode: str, keys: list[str] | None) -> list[dict[str, Any]]:
    if mode == MODE_FINANCE:
        return list_taiwan_finance_rss_sources(keys=keys)
    return list_taiwan_news_rss_sources(keys=keys)


def _filter_items_by_mode(*, mode: str, items: list[dict[str, Any]], source_weight: int = 0) -> list[dict[str, Any]]:
    if mode != MODE_FINANCE:
        return items
    enriched_items = []
    for row in items:
        item = dict(row)
        item['source_weight'] = source_weight
        enriched_items.append(item)
    return filter_finance_items(enriched_items, min_score=2, max_items=MAX_ITEMS_PER_SOURCE)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    # 目的：抓取台灣新聞 RSS 並輸出可直接閱讀的摘要。
    # 為什麼：讓客服代理可用 executable 技能直接取得最新新聞，不必依賴外部不穩定聚合站。
    raw = payload if isinstance(payload, dict) else {}
    mode = _resolve_mode(raw)
    keys = raw.get('sources') if isinstance(raw.get('sources'), list) else None
    top_n_raw = raw.get('top_n', 5)
    try:
        top_n = int(top_n_raw)
    except Exception:
        top_n = 5
    top_n = max(1, min(MAX_ITEMS_PER_SOURCE, top_n))

    selected = _select_sources(mode=mode, keys=keys)
    selected = selected[:MAX_SOURCES]
    if not selected:
        return {
            'ok': False,
            'error': 'no_available_sources',
            'text': '目前沒有可用的台灣新聞來源。' if mode == MODE_GENERAL else '目前沒有可用的台灣財經新聞來源。',
        }

    report_lines: list[str] = []
    report_title = '台灣新聞 RSS 摘要：' if mode == MODE_GENERAL else '台灣財經新聞 RSS 摘要：'
    report_lines.append(report_title)

    source_results: list[dict[str, Any]] = []
    for source in selected:
        source_name = source['name']
        source_url = source['url']
        source_weight = int(source.get('weight') or 0)
        try:
            response = requests.get(
                source_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={'User-Agent': 'dyagent/1.0'},
            )
            response.raise_for_status()
            items = _extract_rss_items(response.text, top_n=top_n)
            items = _filter_items_by_mode(mode=mode, items=items, source_weight=source_weight)
            source_results.append(
                {
                    'source_key': source['key'],
                    'source_name': source_name,
                    'url': source_url,
                    'items': items,
                    'ok': True,
                }
            )
            report_lines.append(f"\n【{source_name}】")
            if not items:
                report_lines.append('- 無可解析項目')
            for idx, item in enumerate(items, start=1):
                score_label = ''
                if mode == MODE_FINANCE and item.get('finance_score') is not None:
                    score_label = f"（財經分數: {item.get('finance_score')}）"
                report_lines.append(f"{idx}. {item.get('title', '')}{score_label}")
                if item.get('link'):
                    report_lines.append(f"   {item['link']}")
        except Exception as error:
            source_results.append(
                {
                    'source_key': source['key'],
                    'source_name': source_name,
                    'url': source_url,
                    'items': [],
                    'ok': False,
                    'error': str(error),
                }
            )
            report_lines.append(f"\n【{source_name}】")
            report_lines.append(f"- 抓取失敗：{error}")

    return {
        'ok': True,
        'mode': 'taiwan_news_rss' if mode == MODE_GENERAL else 'taiwan_finance_news_rss',
        'news_mode': mode,
        'top_n': top_n,
        'sources': source_results,
        'text': '\n'.join(report_lines),
    }
