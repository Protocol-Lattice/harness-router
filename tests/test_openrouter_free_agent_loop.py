from examples.openrouter_free_agent_loop import (
    _goal_requires_mutation,
    _parse_planner_message,
    _target_path_from_goal,
)


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


def test_target_path_from_goal_extracts_explicit_file() -> None:
    assert _target_path_from_goal(
        "refactor examples/basic.py and run tests"
    ) == "examples/basic.py"


def test_target_path_from_goal_handles_backticks() -> None:
    assert _target_path_from_goal(
        "refactor `examples/basic.py` and run tests"
    ) == "examples/basic.py"


def test_goal_requires_mutation_for_refactor() -> None:
    assert _goal_requires_mutation("refactor examples/basic.py and run tests") is True


def test_goal_does_not_require_mutation_for_explanation() -> None:
    assert _goal_requires_mutation("explain examples/basic.py") is False
