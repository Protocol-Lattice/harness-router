from examples.coding_agent import (
    AgentState,
    AgentStats,
    MCTSActionSelector,
    TOOLS,
    _normalize_planner_action,
    _parse_model_object,
)
from harness_router import ActionSummary


def test_parse_mcp_style_tool_call_with_leading_prose() -> None:
    value = _parse_model_object(
        "I'll start by reading the file.\n\n"
        "<mcp:tool_call>\n"
        '<invoke name="read_file">\n'
        '<parameter name="path">coding_agent.py</parameter>\n'
        "</invoke>\n"
        "</mcp:tool_call>"
    )

    assert value == {
        "tool": "read_file",
        "arguments": {"path": "coding_agent.py"},
    }


def test_parse_mcp_style_tool_call_multiple_parameters() -> None:
    value = _parse_model_object(
        "<mcp:tool_call>"
        '<invoke name="replace_text">'
        '<parameter name="path">examples/coding_agent.py</parameter>'
        '<parameter name="old">foo &amp; bar</parameter>'
        '<parameter name="new">baz</parameter>'
        "</invoke>"
        "</mcp:tool_call>"
    )

    assert value == {
        "tool": "replace_text",
        "arguments": {
            "path": "examples/coding_agent.py",
            "old": "foo & bar",
            "new": "baz",
        },
    }


def test_normalize_name_arguments_shape() -> None:
    assert _normalize_planner_action(
        {"name": "read_file", "arguments": {"path": "examples/coding_agent.py"}}
    ) == ("read_file", {"path": "examples/coding_agent.py"})


def test_normalize_nested_function_with_json_arguments() -> None:
    assert _normalize_planner_action(
        {
            "function": {
                "name": "replace_text",
                "arguments": '{"path":"x.py","old":"a","new":"b"}',
            }
        }
    ) == (
        "replace_text",
        {"path": "x.py", "old": "a", "new": "b"},
    )


def test_normalize_tool_call_wrapper_with_parameters() -> None:
    assert _normalize_planner_action(
        {
            "tool_call": {
                "name": "search_code",
                "parameters": {"query": "JevToolRouter"},
            }
        }
    ) == ("search_code", {"query": "JevToolRouter"})


def test_mcts_prefers_verification_after_mutation() -> None:
    stats = AgentStats()
    selector = MCTSActionSelector(
        tools=TOOLS,
        simulations=96,
        max_depth=4,
        exploration=1.41421356237,
        seed=0,
        stats=stats,
    )
    state = AgentState(
        goal="fix a bug",
        history=[
            ActionSummary(tool="read_file", outcome="inspected"),
            ActionSummary(tool="replace_text", outcome="updated file"),
        ],
        tests_passed=False,
    )

    tool, _ = selector.select(state)

    assert tool == "run_tests"
    assert stats.mcts_decisions == 1
    assert stats.mcts_simulations == 96


def test_mcts_finishes_when_tests_already_passed() -> None:
    stats = AgentStats()
    selector = MCTSActionSelector(
        tools=TOOLS,
        simulations=16,
        max_depth=3,
        exploration=1.0,
        seed=0,
        stats=stats,
    )
    state = AgentState(goal="finish task", tests_passed=True)

    tool, _ = selector.select(state)

    assert tool == "finish"
