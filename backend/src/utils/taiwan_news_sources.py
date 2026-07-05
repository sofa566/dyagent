from __future__ import annotations

from typing import TypedDict


class TaiwanRssSource(TypedDict):
    key: str
    name: str
    url: str
    note: str


TAIWAN_NEWS_RSS_SOURCES: tuple[TaiwanRssSource, ...] = (
    {
        "key": "google_news_tw",
        "name": "Google News 台灣總覽",
        "url": "https://news.google.com/rss?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "note": "聚合多來源，更新快",
    },
    {
        "key": "udn_main",
        "name": "聯合新聞網",
        "url": "https://udn.com/rssfeed/news/2/6638?ch=news",
        "note": "綜合新聞",
    },
    {
        "key": "rti",
        "name": "Rti 中央廣播電臺",
        "url": "https://www.rti.org.tw/rss",
        "note": "國內外即時整理",
    },
    {
        "key": "yahoo_tw",
        "name": "Yahoo 新聞台灣",
        "url": "https://tw.news.yahoo.com/rss/",
        "note": "入口型聚合",
    },
    {
        "key": "yahoo_tw_politics",
        "name": "Yahoo 新聞台灣（政治）",
        "url": "https://tw.news.yahoo.com/rss/politics",
        "note": "政治類別",
    },
)


DEFAULT_TAIWAN_RSS_KEYS: tuple[str, ...] = (
    "google_news_tw",
    "udn_main",
    "rti",
    "yahoo_tw",
)


def list_taiwan_news_rss_sources(keys: list[str] | None = None) -> list[TaiwanRssSource]:
    requested = set(keys) if isinstance(keys, list) and keys else set(DEFAULT_TAIWAN_RSS_KEYS)
    return [dict(row) for row in TAIWAN_NEWS_RSS_SOURCES if row["key"] in requested]


def build_taiwan_news_fetch_prompt(keys: list[str] | None = None, top_n: int = 5) -> str:
    # 目的：產生可直接貼給代理者的抓取指令。
    # 為什麼：統一台灣新聞來源清單，避免每次手動整理網址造成遺漏或格式不一致。
    rows = list_taiwan_news_rss_sources(keys=keys)
    limit = top_n if isinstance(top_n, int) and top_n > 0 else 5
    if not rows:
        return "目前沒有可用的台灣新聞 RSS 來源。"
    lines = [
        "請呼叫工具 mcp:fetch 抓以下 RSS，並輸出『來源、前{n}則標題、連結』。".format(n=limit),
        "",
        "來源清單:",
    ]
    for row in rows:
        lines.append(f"- {row['name']}: {row['url']}")
    lines.append("")
    lines.append("回覆請使用繁體中文。")
    return "\n".join(lines)


__all__ = [
    "TaiwanRssSource",
    "TAIWAN_NEWS_RSS_SOURCES",
    "DEFAULT_TAIWAN_RSS_KEYS",
    "list_taiwan_news_rss_sources",
    "build_taiwan_news_fetch_prompt",
]
