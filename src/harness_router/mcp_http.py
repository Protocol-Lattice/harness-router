from __future__ import annotations

import argparse

from .mcp_server import create_mcp_server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Harness Router MCP server.")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/mcp")
    args = parser.parse_args()

    server = create_mcp_server()
    if args.transport == "streamable-http":
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
            stateless_http=True,
            json_response=True,
        )
    else:
        server.run()


if __name__ == "__main__":
    main()
