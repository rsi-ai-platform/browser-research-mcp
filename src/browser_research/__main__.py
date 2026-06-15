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
        # FastMCP's default `allowed_hosts` is ['127.0.0.1:*', 'localhost:*',
        # '[::1]:*'] which rejects any Host header from a real domain with
        # "Invalid Host header" before reaching our handlers. On Cloud Run
        # the inbound Host is *.run.app so we have to relax this.
        try:
            from mcp.server.transport_security import TransportSecuritySettings
            allowed = os.environ.get("ALLOWED_HOSTS")
            if allowed:
                hosts = [h.strip() for h in allowed.split(",") if h.strip()]
                mcp.settings.transport_security = TransportSecuritySettings(
                    enable_dns_rebinding_protection=True,
                    allowed_hosts=hosts,
                    allowed_origins=[f"https://{h}" for h in hosts],
                )
            else:
                # Upstream auth (ANTHROPIC_API_KEY) is the only thing
                # protecting the surface; rebinding protection is irrelevant
                # for our deployment.
                mcp.settings.transport_security = TransportSecuritySettings(
                    enable_dns_rebinding_protection=False,
                )
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "Could not adjust transport_security: %s", e)

        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # Match authority-web-search defaults: stateless HTTP + JSON response
        # for compatibility with one-shot JSON-RPC clients that don't track
        # Mcp-Session-Id between requests.
        mcp.settings.stateless_http = True
        mcp.settings.json_response = True
        # Manual uvicorn so the hybrid auth middleware can attach.
        import uvicorn
        from ._auth import install_auth_middleware
        app = mcp.streamable_http_app()
        install_auth_middleware(
            app, service_url=os.environ.get("SERVICE_URL", "<unset>"))
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        print(f"unknown transport: {args.transport}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
