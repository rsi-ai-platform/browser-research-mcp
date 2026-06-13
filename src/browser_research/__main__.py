"""Entry point — mirrors authority-web-search-mcp's CLI surface."""
from __future__ import annotations

import argparse
import logging
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="browser-research")
    parser.add_argument(
        "--transport",
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        choices=["stdio", "sse", "streamable-http"],
    )
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "7862")))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from .server import mcp

    if args.transport == "stdio":
        mcp.run("stdio")
    elif args.transport == "sse":
        # FastMCP picks up host/port from settings.
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run("sse")
    elif args.transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # Match authority-web-search defaults: stateless HTTP + relaxed DNS rebinding
        # for compatibility with multiple MCP clients on different hosts.
        mcp.settings.stateless_http = True
        mcp.settings.json_response = True
        mcp.run("streamable-http")
    else:
        print(f"unknown transport: {args.transport}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
