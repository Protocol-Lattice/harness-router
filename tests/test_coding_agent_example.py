from examples.coding_agent import _parse_model_object


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
