"""Analyst review: the human in the loop.

Reviews are only reachable through the analyst console's HTTP API, never through MCP, so an
agent cannot approve its own actions. Each decision is stored as
(:Review {id, decision, note, ts, reviewer})-[:REVIEWS]->(:Case | :Action) and changes the graph:

  case    confirm   case.status = 'confirmed'; signal and rule tallies credited
          overturn  case.status = 'overturned'; tallies debited; the case's freezes are reversed
  action  approve   a pending or blocked freeze is executed (an analyst override is recorded)
          reject    an executed freeze is reversed, a pending one is dropped
"""
from __future__ import annotations

from . import db, learning, policies


class ReviewError(ValueError):
    pass


def _review(target_id: str, decision: str, note: str) -> str:
    rid = policies.new_id("REV")
    db.write("""
        MATCH (t {id: $tid})
        CREATE (r:Review {id: $rid, decision: $decision, note: $note, ts: timestamp(), reviewer: 'analyst'})-[:REVIEWS]->(t)""",
             {"tid": target_id, "rid": rid, "decision": decision, "note": (note or "")[:1000]})
    return rid


def _unfreeze(action_id: str, account_ids: list[str]) -> list[str]:
    """Return accounts to active unless another executed freeze from a live case still holds them."""
    rows = db.write("""
        UNWIND $ids AS aid
        MATCH (a:Account {id: aid})
        OPTIONAL MATCH (c:Case)-[:TOOK]->(x:Action {kind: 'freeze', status: 'done'})-[:ON]->(a)
        WHERE x.id <> $xid AND c.status <> 'overturned'
        WITH a, count(x) AS others
        WHERE others = 0 AND a.status = 'frozen'
        SET a.status = 'active'
        RETURN a.id AS id""", {"ids": account_ids, "xid": action_id})
    return [r["id"] for r in rows]


def review_case(case_id: str, decision: str, note: str = "") -> dict:
    if decision not in ("confirm", "overturn"):
        raise ReviewError("decision must be confirm or overturn")
    row = db.read("MATCH (c:Case {id: $id}) RETURN c.status AS status", {"id": case_id})
    if not row:
        raise ReviewError(f"unknown case {case_id}")
    if row[0]["status"] in ("confirmed", "overturned"):
        raise ReviewError(f"case {case_id} was already reviewed ({row[0]['status']})")
    rid = _review(case_id, decision, note)
    confirmed = decision == "confirm"
    db.write("MATCH (c:Case {id: $id}) SET c.status = $s, c.reviewed = timestamp()",
             {"id": case_id, "s": "confirmed" if confirmed else "overturned"})
    learning.record_review(case_id, confirmed)
    released = []
    if not confirmed:
        freezes = db.read("""
            MATCH (:Case {id: $id})-[:TOOK]->(x:Action {kind: 'freeze', status: 'done'})-[:ON]->(a:Account)
            RETURN x.id AS xid, collect(a.id) AS ids""", {"id": case_id})
        for f in freezes:
            db.write("MATCH (x:Action {id: $xid}) SET x.status = 'reversed'", {"xid": f["xid"]})
            released += _unfreeze(f["xid"], f["ids"])
        db.write("MATCH (:Case {id: $id})-[:TOOK]->(x:Action {status: 'pending_analyst'}) SET x.status = 'rejected'",
                 {"id": case_id})
        db.write("MATCH (:Case {id: $id})-[:RAISED_FROM]->(al:Alert) SET al.disposition = 'overturned_by_analyst'",
                 {"id": case_id})
    return {"review": rid, "case": case_id, "status": "confirmed" if confirmed else "overturned",
            "unfrozen": sorted(set(released))}


def review_action(action_id: str, decision: str, note: str = "") -> dict:
    if decision not in ("approve", "reject"):
        raise ReviewError("decision must be approve or reject")
    row = db.read("""
        MATCH (x:Action {id: $id}) OPTIONAL MATCH (x)-[:ON]->(a:Account)
        RETURN x.kind AS kind, x.status AS status, collect(a.id) AS ids""", {"id": action_id})
    if not row or row[0]["kind"] not in ("freeze", "freeze_recommended"):
        raise ReviewError(f"{action_id} is not a freeze action")
    x = row[0]
    if x["status"] in ("reversed", "rejected"):
        raise ReviewError(f"{action_id} is already {x['status']}")
    rid = _review(action_id, decision, note)
    out = {"review": rid, "action": action_id}
    if decision == "approve":
        if x["status"] != "done":
            db.write("UNWIND $ids AS aid MATCH (a:Account {id: aid}) SET a.status = 'frozen'", {"ids": x["ids"]})
            db.write("MATCH (x:Action {id: $id}) SET x.status = 'done', x.kind = 'freeze', x.approved_by = 'analyst', "
                     "x.override = $override", {"id": action_id, "override": x["status"] == "blocked"})
        out.update(status="done", frozen=x["ids"], override=x["status"] == "blocked")
    else:
        released = []
        if x["status"] == "done":
            db.write("MATCH (x:Action {id: $id}) SET x.status = 'reversed'", {"id": action_id})
            released = _unfreeze(action_id, x["ids"])
            out["status"] = "reversed"
        else:
            db.write("MATCH (x:Action {id: $id}) SET x.status = 'rejected'", {"id": action_id})
            out["status"] = "rejected"
        out["unfrozen"] = released
    return out
