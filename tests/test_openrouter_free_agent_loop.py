from examples.openrouter_free_agent_loop import (
    AgentState,
    Workspace,
    _goal_requires_mutation,
    _history_text,
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


def test_replace_text_noop_does_not_mark_file_changed(tmp_path) -> None:
    target = tmp_path / "example.py"
    target.write_text("value = 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path, apply=True, test_timeout=1.0)
    state = AgentState(goal="refactor example.py")

    result = workspace.execute(
        "replace_text",
        {"path": "example.py", "old": "value = 1", "new": "value = 1"},
        state,
    )

    assert result.startswith("error: no-op mutation")
    assert workspace.changed_files == set()
    assert target.read_text(encoding="utf-8") == "value = 1\n"


def test_replace_text_real_change_reports_diff(tmp_path) -> None:
    target = tmp_path / "example.py"
    target.write_text("value = 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path, apply=True, test_timeout=1.0)
    state = AgentState(goal="refactor example.py")

    result = workspace.execute(
        "replace_text",
        {"path": "example.py", "old": "value = 1", "new": "value = 2"},
        state,
    )

    assert "updated example.py" in result
    assert "-value = 1" in result
    assert "+value = 2" in result
    assert workspace.changed_files == {"example.py"}
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_write_file_noop_does_not_mark_file_changed(tmp_path) -> None:
    target = tmp_path / "example.py"
    target.write_text("value = 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path, apply=True, test_timeout=1.0)
    state = AgentState(goal="rewrite example.py")

    result = workspace.execute(
        "write_file",
        {"path": "example.py", "content": "value = 1\n"},
        state,
    )

    assert result.startswith("error: no-op mutation")
    assert workspace.changed_files == set()


def test_safe_path_allows_absolute_path_only_inside_workspace(tmp_path) -> None:
    target = tmp_path / "example.py"
    target.write_text("value = 1\n", encoding="utf-8")
    workspace = Workspace(tmp_path, apply=True, test_timeout=1.0)

    assert workspace._safe_path(str(target)) == target.resolve()


def test_read_file_returns_full_large_file(tmp_path) -> None:
    content = "x" * 250_000
    target = tmp_path / "large.txt"
    target.write_text(content, encoding="utf-8")
    workspace = Workspace(tmp_path, apply=True, test_timeout=1.0)
    state = AgentState(goal="read large.txt")

    result = workspace.execute(
        "read_file",
        {"path": "large.txt"},
        state,
    )

    assert result == content
    assert len(result) == 250_000


def test_history_text_keeps_full_outcome() -> None:
    content = "a" * 20_000
    history = [
        __import__("harness_router").ActionSummary(
            tool="read_file",
            outcome=content,
        )
    ]

    rendered = _history_text(history)

    assert content in rendered
    assert len(rendered) > 20_000
