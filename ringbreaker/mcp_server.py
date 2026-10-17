"""MCP server exposing the investigation tool catalogue.

    python -m ringbreaker.mcp_server          # stdio, used by the RingBreaker agent
    python -m ringbreaker.mcp_server --http   # streamable HTTP on :8765 for other MCP clients

Any MCP client (the RingBreaker agent, an IDE, a desktop app) gets the same fixed set of
reviewed graph queries and actions; none of them can run arbitrary Cypher.
"""
from __future__ import annotations

import json
import sys

from mcp.server.fastmcp import FastMCP

from . import tools as T

mcp = FastMCP("ringbreaker", instructions=(
    "Graph tools for investigating financial-crime alerts in a bank's FalkorDB graph. "
    "Read tools trace money, shared identity elements and communities; action tools open cases, "
    "record findings, freeze accounts and close alerts."))


def _wrap(fn):
    def call(**kwargs):
        try:
            return json.loads(json.dumps(fn(**kwargs), default=str))
        except T.ToolError as e:
            return {"error": str(e)}
    call.__name__ = fn.__name__
    call.__doc__ = fn.__doc__
    call.__signature__ = __import__("inspect").signature(fn)
    call.__annotations__ = fn.__annotations__
    return call


for _fn in T.ALL_TOOLS.values():
    mcp.add_tool(_wrap(_fn), name=_fn.__name__, description=" ".join((_fn.__doc__ or "").split()))


if __name__ == "__main__":
    if "--http" in sys.argv:
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = 8765
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
