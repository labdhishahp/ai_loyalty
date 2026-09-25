"""Run the L-Mart MCP server over stdio.

For local clients -- Claude Desktop, the MCP Inspector -- where the client
launched this process and the operating system has already established who you
are. Read-only unless MCP_ALLOW_WRITES is set.

  python -m mcp_server
"""

from __future__ import annotations

import logging

from .auth import stdio_principal
from .server import build

if __name__ == "__main__":
    # stdout carries the protocol, so logs must go to stderr or the stream is
    # corrupted and the client sees malformed JSON-RPC.
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        handlers=[logging.StreamHandler()])
    principal = stdio_principal()
    logging.getLogger("lmart.mcp").info(
        "stdio session as %s (%s) scopes=%s",
        principal.actor, principal.role, sorted(principal.scopes))
    build(lambda: principal).run(transport="stdio")
