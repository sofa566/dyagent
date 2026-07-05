from __future__ import annotations

from typing import Any, TypedDict
import re


class TaiwanFinanceRssSource(TypedDict):
    key: str
    name: str
    url: str
    note: str
    weight: int


TAIWAN_FINANCE_RSS_SOURCES: tuple[TaiwanFinanceRssSource, ...] = (
    {
        "key": "google_business_tw",
        "name": "Google News 商業主題（台灣）",
        "url": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "note": "更新快，來源多",
        "weight": 1,
    },
    {
        "key": "ctee_feed",
        "name": "工商時報",
        "url": "https://news.google.com/rss/search?q=site:ctee.com.tw+財經&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "note": "透過 Google 聚合工商時報財經新聞（降低直連封鎖失敗）",
        "weight": 3,
    },
    {
        "key": "udn_money",
        "name": "經濟日報（聯合財經）",
        "url": "https://money.udn.com/rssfeed/news/1001/5590?ch=news",
        "note": "台股與產業鏈消息密集",
        "weight": 3,
    },
    {
        "key": "cnyes_tw",
        "name": "中時財經（Google 聚合）",
        "url": "https://news.google.com/rss/search?q=site:chinatimes.com+財經&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
        "note": "替代原鉅亨 RSS（原連結 404）",
        "weight": 2,
    },
    {
        "key": "yahoo_tw_finance",
        "name": "Yahoo 財經（台灣）",
        "url": "https://tw.stock.yahoo.com/rss",
        "note": "入口型財經聚合",
        "weight": 1,
    },
)


DEFAULT_TAIWAN_FINANCE_RSS_KEYS: tuple[str, ...] = (
    "google_business_tw",
    "ctee_feed",
    "udn_money",
    "cnyes_tw",
)


FINANCE_POSITIVE_KEYWORDS: tuple[str, ...] = (
    "台股",
    "美股",
    "港股",
    "日股",
    "加權指數",
    "那斯達克",
    "道瓊",
    "標普",
    "股價",
    "殖利率",
    "本益比",
    "EPS",
    "財報",
    "營收",
    "法說",
    "外資",
    "投信",
    "自營商",
    "ETF",
    "基金",
    "匯率",
    "新台幣",
    "美元",
    "央行",
    "升息",
    "降息",
    "CPI",
    "通膨",
    "GDP",
    "失業率",
    "景氣",
    "半導體",
    "AI 伺服器",
    "供應鏈",
    "併購",
    "公司債",
    "國際油價",
)


FINANCE_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "娛樂",
    "影劇",
    "八卦",
    "藝人",
    "星座",
    "體育",
    "球員",
    "球賽",
    "社會",
    "命案",
    "車禍",
    "氣象",
    "颱風",
    "美食",
    "旅遊",
)


def list_taiwan_finance_rss_sources(keys: list[str] | None = None) -> list[TaiwanFinanceRssSource]:
    requested = set(keys) if isinstance(keys, list) and keys else set(DEFAULT_TAIWAN_FINANCE_RSS_KEYS)
    return [dict(row) for row in TAIWAN_FINANCE_RSS_SOURCES if row["key"] in requested]


def _normalize_for_dedup(text: str) -> str:
    lowered = str(text or "").lower().strip()
    alnum_only = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", lowered)
    return alnum_only


def score_finance_relevance(*, title: str, summary: str = "", source_weight: int = 0) -> int:
    # 目的：依標題與摘要估算財經相關性分數。
    # 為什麼：綜合來源常混入非財經新聞，需要可調整的規則分數避免主題漂移。
    search_text = f"{str(title or '')} {str(summary or '')}".lower()
    score = int(source_weight or 0)

    for keyword in FINANCE_POSITIVE_KEYWORDS:
        if keyword.lower() in search_text:
            score += 2

    for keyword in FINANCE_NEGATIVE_KEYWORDS:
        if keyword.lower() in search_text:
            score -= 2

    if "財經" in search_text:
        score += 2
    if "經濟" in search_text:
        score += 1
    if "股票" in search_text:
        score += 2
    return score


def filter_finance_items(
    items: list[dict[str, Any]],
    *,
    min_score: int = 2,
    max_items: int = 30,
) -> list[dict[str, Any]]:
    # 目的：篩出財經新聞並去除重複標題。
    # 為什麼：聚合 RSS 易重複且主題混雜，先在 utils 層集中處理可讓技能輸出更穩定。
    if not isinstance(items, list) or not items:
        return []

    seen_titles: set[str] = set()
    ranked: list[dict[str, Any]] = []

    for row in items:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        normalized_title = _normalize_for_dedup(title)
        if not normalized_title or normalized_title in seen_titles:
            continue

        summary = str(row.get("summary") or row.get("description") or "").strip()
        source_weight = int(row.get("source_weight") or 0)
        score = score_finance_relevance(title=title, summary=summary, source_weight=source_weight)
        if score < min_score:
            continue

        seen_titles.add(normalized_title)
        enriched = dict(row)
        enriched["finance_score"] = score
        ranked.append(enriched)

    ranked.sort(key=lambda x: int(x.get("finance_score") or 0), reverse=True)
    limit = max(1, int(max_items or 30))
    return ranked[:limit]


def build_taiwan_finance_fetch_prompt(keys: list[str] | None = None, top_n: int = 8) -> str:
    # 目的：產生財經新聞抓取提示詞。
    # 為什麼：統一來源與輸出要求，讓代理者可穩定產出可比較的每日財經摘要。
    rows = list_taiwan_finance_rss_sources(keys=keys)
    limit = top_n if isinstance(top_n, int) and top_n > 0 else 8
    if not rows:
        return "目前沒有可用的台灣財經 RSS 來源。"

    lines = [
        "請抓取以下財經 RSS 並整理重點。",
        f"每個來源最多保留 {limit} 則，優先保留財經相關分數較高的內容。",
        "",
        "來源清單:",
    ]
    for row in rows:
        lines.append(f"- {row['name']}: {row['url']}（權重={row['weight']}）")

    lines.extend(
        [
            "",
            "輸出格式：",
            "1) 標題",
            "2) 來源",
            "3) 連結",
            "4) 一句摘要",
            "5) 影響面向（台股/美股/匯率/產業）",
            "",
            "回覆請使用繁體中文。",
        ]
    )
    return "\n".join(lines)


def build_finance_agent_system_prompt() -> str:
    # 目的：提供客服/資訊型代理者可直接套用的財經新聞系統提示。
    # 為什麼：將財經範圍、輸出格式與風險提醒集中，避免各代理者 prompt 漂移。
    lines = [
        "你是台灣財經新聞助理。",
        "任務：整理可驗證的財經重點，優先使用財經專屬來源與可追溯連結。",
        "",
        "規則：",
        "1) 優先關注市場、產業、公司財報、政策與總經資料。",
        "2) 若內容偏娛樂/社會/體育，除非與資本市場有直接關聯，否則排除。",
        "3) 每則新聞必須附來源與連結，無連結不列入重點。",
        "4) 避免投資建議語氣，改用『可能影響』與『需持續觀察』描述。",
        "",
        "輸出格式：",
        "- 今日重點（3-5 則）",
        "- 每則：標題｜來源｜連結｜一句摘要｜可能影響",
        "- 最後補充：今日市場觀察（2-3 點）",
        "",
        "回覆請使用繁體中文。",
    ]
    return "\n".join(lines)


__all__ = [
    "TaiwanFinanceRssSource",
    "TAIWAN_FINANCE_RSS_SOURCES",
    "DEFAULT_TAIWAN_FINANCE_RSS_KEYS",
    "FINANCE_POSITIVE_KEYWORDS",
    "FINANCE_NEGATIVE_KEYWORDS",
    "list_taiwan_finance_rss_sources",
    "score_finance_relevance",
    "filter_finance_items",
    "build_taiwan_finance_fetch_prompt",
    "build_finance_agent_system_prompt",
]
