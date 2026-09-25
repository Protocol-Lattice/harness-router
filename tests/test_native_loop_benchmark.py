from benchmarks.native_loop import _parse_json_object


def test_parse_tagged_openrouter_tool_call() -> None:
    value = _parse_json_object(
        "<|tool_call_start|>[read_file(path='src/retry.py')]<|tool_call_end|>"
    )

    assert value == {
        "tool": "read_file",
        "arguments": {"path": "src/retry.py"},
    }


def test_parse_tagged_openrouter_tool_call_with_multiple_arguments() -> None:
    value = _parse_json_object(
        "<|tool_call_start|>[replace_text("
        "path='src/retry.py', old='range(max_attempts + 1)', "
        "new='range(max_attempts)')]<|tool_call_end|>"
    )

    assert value == {
        "tool": "replace_text",
        "arguments": {
            "path": "src/retry.py",
            "old": "range(max_attempts + 1)",
            "new": "range(max_attempts)",
        },
    }


def test_parse_regular_json_response() -> None:
    value = _parse_json_object(
        '{"tool":"run_tests","arguments":{}}'
    )

    assert value == {"tool": "run_tests", "arguments": {}}
