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
            current_weather = {
                "temperature": current.get("temperature_2m"),
                "windspeed": current.get("wind_speed_10m"),
                "weathercode": current.get("weather_code"),
            }

            return {
                "ok": True,
                "location": {
                    "query": q,
                    "name": resolved_name,
                    "country": country,
                    "latitude": lat,
                    "longitude": lon,
                },
                "current": current,
                "current_weather": current_weather,
                "source": "open-meteo",
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}
