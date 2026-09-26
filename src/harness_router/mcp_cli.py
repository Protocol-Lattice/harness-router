from __future__ import annotations


def main() -> None:
    try:
        from .mcp_server import main as run_server
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == "mcp" or exc.name.startswith("mcp.")):
            raise SystemExit(
                'MCP support is optional. Install it with: pip install "harness-router[mcp]"'
            ) from None
        raise

    run_server()


if __name__ == "__main__":
    main()
