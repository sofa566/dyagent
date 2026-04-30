"""Open-Meteo 天氣查詢技能（Python handler）。

Handler:
    get_weather(payload: dict) -> dict

輸入：
    {
      "q": "台北"   # 城市名稱（繁中/英文皆可）
    }
"""

from __future__ import annotations

from typing import Any
import httpx


CITY_ALIAS = {
    "台北": "Taipei",
    "臺北": "Taipei",
    "新北": "New Taipei",
    "桃園": "Taoyuan",
    "台中": "Taichung",
    "臺中": "Taichung",
    "台南": "Tainan",
    "臺南": "Tainan",
    "高雄": "Kaohsiung",
    "基隆": "Keelung",
    "新竹": "Hsinchu",
    "嘉義": "Chiayi",
}

WEATHER_CODE_TEXT = {
    0: "晴朗",
    1: "大致晴",
    2: "局部多雲",
    3: "陰天",
    45: "有霧",
    48: "有霧並有霜",
    51: "毛毛雨",
    53: "中度毛毛雨",
    55: "強烈毛毛雨",
    56: "凍毛毛雨",
    57: "強烈凍毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "凍雨",
    67: "強烈凍雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "陣雨",
    81: "中度陣雨",
    82: "強烈陣雨",
    85: "陣雪",
    86: "強烈陣雪",
    95: "雷雨",
    96: "雷雨伴隨小冰雹",
    99: "雷雨伴隨大冰雹",
}


def _resolve_weather_text(weather_code: Any) -> str:
    """目的：將 Open-Meteo 天氣代碼轉為繁體中文描述。
    為什麼：讓最終回覆直接顯示可讀天氣敘述，避免使用者看到難懂代碼。
    """
    try:
        code_int = int(weather_code)
    except Exception:
        return "未知天氣"
    return WEATHER_CODE_TEXT.get(code_int, "未知天氣")


def get_weather(payload: dict[str, Any]) -> dict[str, Any]:
    q = str((payload or {}).get("q") or "").strip()
    if not q:
        return {"ok": False, "error": "missing_q"}

    query = CITY_ALIAS.get(q, q)
    timeout = 10.0

    try:
        with httpx.Client(timeout=timeout) as client:
            geo = client.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={
                    "name": query,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
            )
            geo.raise_for_status()
            g = geo.json()
            results = g.get("results") if isinstance(g, dict) else None
            if not isinstance(results, list) or not results:
                return {"ok": False, "error": f"city_not_found: {q}"}

            first = results[0] if isinstance(results[0], dict) else {}
            lat = first.get("latitude")
            lon = first.get("longitude")
            resolved_name = first.get("name") or q
            country = first.get("country") or ""

            if lat is None or lon is None:
                return {"ok": False, "error": "geocoding_missing_latlon"}

            wx = client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,wind_speed_10m,weather_code",
                    "timezone": "Asia/Taipei",
                },
            )
            wx.raise_for_status()
            w = wx.json()

            current = w.get("current") if isinstance(w, dict) else None
            if not isinstance(current, dict):
                return {"ok": False, "error": "missing_current_weather"}

            # 與既有 chat_router 格式化器相容：提供 current_weather 別名
            temperature = current.get("temperature_2m")
            windspeed = current.get("wind_speed_10m")
            weather_code = current.get("weather_code")
            weather_text = _resolve_weather_text(weather_code)

            current_weather = {
                "temperature": temperature,
                "windspeed": windspeed,
                "weather_text": weather_text,
            }

            summary_parts = [f"{resolved_name}目前天氣{weather_text}"]
            if temperature is not None:
                summary_parts.append(f"溫度約 {temperature}°C")
            if windspeed is not None:
                summary_parts.append(f"風速約 {windspeed} km/h")
            summary_text = "，".join(summary_parts) + "。"

            return {
                "ok": True,
                "text": summary_text,
                "location": {
                    "query": q,
                    "name": resolved_name,
                    "country": country,
                    "latitude": lat,
                    "longitude": lon,
                },
                "current": {
                    "time": current.get("time"),
                    "interval": current.get("interval"),
                    "temperature_2m": temperature,
                    "wind_speed_10m": windspeed,
                    "weather_text": weather_text,
                },
                "current_weather": current_weather,
                "source": "open-meteo",
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}
