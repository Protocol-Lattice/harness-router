from harness_router.cli import build_parser


def test_cli_parser_supports_route() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "route",
            "--goal",
            "Fix tests",
            "--tools-json",
            '[{"name":"read_file","description":"Read a file"}]',
        ]
    )
    assert args.command == "route"
    assert args.goal == "Fix tests"


def test_cli_without_subcommand_is_valid() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.command is None
