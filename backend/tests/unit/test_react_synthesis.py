from src.services.react_synthesis import ReActSynthesis


def test_react_action_then_final():
    plans = iter(
        [
            '{"kind":"action","tool":"open_meteo","arguments":{"q":"台北"}}',
            '{"kind":"final","answer":"台北目前 25°C，風速 3m/s。"}',
        ]
    )

    def fake_llm(prompt: str, tier: str | None) -> str:
        _ = prompt, tier
        return next(plans)

    def fake_tool(tool: str, payload: dict):
        assert tool == "open_meteo"
        assert payload == {"q": "台北"}
        return {"ok": True, "result": {"current_weather": {"temperature": 25, "windspeed": 3, "weathercode": 0}}}

    syn = ReActSynthesis(llm_complete=fake_llm, call_tool=fake_tool, max_steps=3)
    out = syn.run(user_message="今天台北天氣", allowed_tools=["open_meteo"], tier="cloud")
    assert out == "台北目前 25°C，風速 3m/s。"


def test_react_weather_keyword_fallback_when_planner_invalid_json():
    def fake_llm(prompt: str, tier: str | None) -> str:
        _ = prompt, tier
        return "not-json"

    def fake_tool(tool: str, payload: dict):
        assert tool == "weather_tool"
        assert payload.get("q") == "台北"
        return {"ok": True, "result": {"current_weather": {"temperature": 22, "windspeed": 4, "weathercode": 1}}}

    syn = ReActSynthesis(llm_complete=fake_llm, call_tool=fake_tool, max_steps=2)
    out = syn.run(user_message="請問今天台北的天氣？", allowed_tools=["weather_tool"], tier=None)
    assert isinstance(out, str)
    assert "台北" in out
    assert "氣溫約 22°C" in out
