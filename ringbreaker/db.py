"""FalkorDB connection. Uses FalkorDB Cloud / server when FALKORDB_HOST is set,
otherwise an embedded FalkorDB (falkordblite) file for local development."""
from __future__ import annotations

import os
from functools import lru_cache

GRAPH = os.getenv("RB_GRAPH", "ringbreaker")


@lru_cache(maxsize=1)
def _client():
    host = os.getenv("FALKORDB_HOST")
    if host:
        from falkordb import FalkorDB
        return FalkorDB(host=host, port=int(os.getenv("FALKORDB_PORT", "6379")),
                        username=os.getenv("FALKORDB_USERNAME") or None,
                        password=os.getenv("FALKORDB_PASSWORD") or None,
                        ssl=os.getenv("FALKORDB_SSL", "").lower() in ("1", "true", "yes"))
    try:
        from redislite.falkordb_client import FalkorDB
    except ImportError as e:
        raise RuntimeError("Set FALKORDB_HOST to a FalkorDB server, or pip install -r requirements-dev.txt "
                           "for the embedded database.") from e
    return FalkorDB(os.getenv("FALKORDB_LITE_PATH", "data/ringbreaker.db"))


def graph(name: str | None = None):
    return _client().select_graph(name or GRAPH)


def rows(result) -> list[dict]:
    """Turn a FalkorDB result into a list of dicts keyed by column alias."""
    cols = [c[1] if isinstance(c, (list, tuple)) else c for c in result.header]
    cols = [c.decode() if isinstance(c, bytes) else c for c in cols]
    return [dict(zip(cols, r)) for r in result.result_set]


def read(cypher: str, params: dict | None = None, name: str | None = None) -> list[dict]:
    return rows(graph(name).ro_query(cypher, params or {}))


def write(cypher: str, params: dict | None = None, name: str | None = None) -> list[dict]:
    return rows(graph(name).query(cypher, params or {}))
