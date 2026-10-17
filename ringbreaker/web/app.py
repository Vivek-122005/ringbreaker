"""Analyst console: alert queue, live investigation stream, evidence graph, case report.

    uvicorn ringbreaker.web.app:app --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import db
from ..agent import investigate, provider

app = FastAPI(title="RingBreaker")
STATIC = Path(__file__).parent / "static"
_lock = asyncio.Lock()  # one investigation writes to the graph at a time
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _node_info(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    return db.read("""
        MATCH (n) WHERE n.id IN $ids
        RETURN n.id AS id, labels(n)[0] AS label,
               coalesce(n.name, n.os, n.asn, n.line, n.rule, n.typology, n.text, n.category, '') AS caption,
               n.status AS status, n.kind AS kind, n.category AS category""", {"ids": ids})


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    p = provider()
    counts = db.read("""
        MATCH (al:Alert) WITH count(al) AS alerts, sum(CASE WHEN al.status = 'open' THEN 1 ELSE 0 END) AS open
        OPTIONAL MATCH (c:Case) WITH alerts, open, count(c) AS cases
        OPTIONAL MATCH (a:Account {status: 'frozen'})
        RETURN alerts, toInteger(open) AS open, cases, count(a) AS frozen""")[0]
    stats = db.read("MATCH (n) RETURN count(n) AS nodes")[0]
    return {"planner": f"{p[0]} / {p[3]}" if p else "playbook", "llm": bool(p), **counts, **stats}


@app.get("/api/alerts")
def alerts():
    return db.read("""
        MATCH (al:Alert)-[:FLAGS]->(a:Account)<-[:OWNS]-(p:Person)
        RETURN al.id AS id, al.rule AS rule, al.detail AS detail, al.status AS status,
               al.disposition AS disposition, a.id AS account, p.name AS owner, a.status AS account_status
        ORDER BY CASE al.status WHEN 'open' THEN 0 ELSE 1 END, al.id""")


@app.get("/api/cases")
def cases():
    return db.read("""
        MATCH (c:Case)-[:RAISED_FROM]->(al:Alert)
        OPTIONAL MATCH (c)-[:INCLUDES]->(a:Account)
        RETURN c.id AS id, c.typology AS typology, c.risk AS risk, c.summary AS summary, al.id AS alert,
               count(a) AS accounts ORDER BY c.created DESC""")


@app.get("/api/investigate/{alert_id}")
async def run(alert_id: str, planner: str = "auto"):
    if planner not in ("auto", "llm", "playbook"):
        raise HTTPException(400, "planner must be auto, llm or playbook")
    if not db.read("MATCH (al:Alert {id: $id}) RETURN al.id AS id", {"id": alert_id}):
        raise HTTPException(404, "unknown alert")

    async def stream():
        async with _lock:
            known: set[str] = set()
            async for ev in investigate(alert_id, planner):
                ev_nodes = (ev.get("evidence") or {}).get("nodes", [])
                if ev["type"] == "verdict":
                    ev_nodes = list({n for e in ev.get("graph", []) for n in (e[0], e[2])})
                new = [n for n in ev_nodes if n not in known]
                known.update(new)
                if new:
                    ev["nodes"] = _node_info(new)
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        yield "data: {\"type\": \"done\"}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/reset")
async def reset():
    """Rebuild the demo bank from scratch (same seed), clearing cases, findings and freezes."""
    if os.getenv("RB_ALLOW_RESET", "1") != "1":
        raise HTTPException(403, "reset disabled")
    from ..load import load
    async with _lock:
        await asyncio.to_thread(load)
    return {"ok": True}
