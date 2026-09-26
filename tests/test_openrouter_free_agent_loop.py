from examples.openrouter_free_agent_loop import _parse_planner_message


def test_parse_free_model_text_tool_batch_uses_first_tool() -> None:
    message = {
        "content": (
            '[\n'
            '{"name": "read_file", "parameters": {"path": "pyproject.toml"}},\n'
            '{"name": "list_files", "parameters": {"path": "tests"}}\n'
            ']'
        )
    }

    assert _parse_planner_message(message) == (
        "read_file",
        {"path": "pyproject.toml"},
    )


def test_parse_malformed_free_model_batch_uses_first_decodable_tool() -> None:
    message = {
        "content": (
            '[[\n'
            '{"name": "read_file", "parameters": {"path": "pyproject.toml"}},\n'
            '{"name": "list_files", "parameters": {"path": "tests"}}\n'
            ']'
        )
    }

    assert _parse_planner_message(message) == (
        "read_file",
        {"path": "pyproject.toml"},
    )


def test_parse_native_tool_call_still_works() -> None:
    message = {
        "tool_calls": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": '{"path":"README.md"}',
                },
            }
        ]
    }

    assert _parse_planner_message(message) == (
        "read_file",
        {"path": "README.md"},
    )
