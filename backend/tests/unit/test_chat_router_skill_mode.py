from src.services.chat_router import ChatRouter


def test_extract_interactive_skill_result_from_stdout_json():
    router = ChatRouter()
    script_outputs = [
        {
            'script': 'main.js',
            'ok': True,
            'stdout': '{"mode":"ui","step":"step-1","ui":{"entry":"ui/index.html","title":"請假"}}',
            'stderr': '',
        }
    ]
    result = router._extract_interactive_skill_result(script_outputs)
    assert isinstance(result, dict)
    assert result.get('mode') == 'ui'


def test_extract_interactive_skill_result_ignore_non_mode_output():
    router = ChatRouter()
    script_outputs = [
        {
            'script': 'main.js',
            'ok': True,
            'stdout': '{"message":"hello"}',
            'stderr': '',
        }
    ]
    result = router._extract_interactive_skill_result(script_outputs)
    assert result is None
