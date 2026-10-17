"""Must-follow policies, stored in the graph and enforced inside the action tools.

Policies are (:Policy) nodes with their parameters as properties, so they can be read,
versioned and audited like any other bank data. Every action tool evaluates the active
policies that apply to it and records the outcome as
(:Action)-[:GOVERNED_BY {result, detail}]->(:Policy). A planner (playbook or LLM) can read
the policies through the get_policies tool, but it cannot skip them: the checks run on the
server, inside the tool, before anything is written.

Results:
  pass       the policy is satisfied
  escalated  the action is not executed automatically; it waits for an analyst
  fail       the action is blocked; an analyst may still override it (recorded as a review)
"""
from __future__ import annotations

import re
import time

from . import db

POLICIES = [
    {"id": "POL-FREEZE-RISK", "name": "Automatic freeze needs high case risk", "applies_to": "freeze_accounts",
     "min_risk": 0.8, "review_floor": 0.5,
     "rationale": "Freezing payments harms a customer if wrong. Only cases at or above min_risk are frozen "
                  "automatically; cases between review_floor and min_risk go to an analyst; below review_floor "
                  "a freeze is not justified."},
    {"id": "POL-FREEZE-EVIDENCE", "name": "Freeze needs corroborated evidence", "applies_to": "freeze_accounts",
     "min_findings": 3, "min_families": 2,
     "rationale": "A freeze must rest on at least min_findings recorded findings drawn from at least "
                  "min_families independent evidence families (money movement, shared identity, network "
                  "structure), so one noisy signal cannot freeze anyone."},
    {"id": "POL-HUMAN-REVIEW", "name": "Large freezes need an analyst", "applies_to": "freeze_accounts",
     "max_auto_accounts": 15,
     "rationale": "A freeze touching more than max_auto_accounts accounts has a large blast radius and is "
                  "held for analyst approval even when risk is high."},
    {"id": "POL-RESPECT-ANALYST", "name": "Do not override an analyst's clearance", "applies_to": "freeze_accounts",
     "rationale": "If an analyst overturned a case on an account or reversed a freeze on it, a new automatic freeze "
                  "of that account is held for analyst approval instead of executing, so the agent never silently "
                  "undoes a human decision."},
    {"id": "POL-CASE-CONNECTED", "name": "Case accounts must be connected to the alert", "applies_to": "open_case",
     "max_hops": 4,
     "rationale": "Every account put into a case must be reachable from the alerted account through identity "
                  "or money edges within max_hops. Accounts with no graph connection are left out of the case."},
    {"id": "POL-FP-REASON", "name": "False-positive closures cite a graph fact", "applies_to": "close_alert",
     "min_reason_chars": 20,
     "rationale": "Closing an alert as a false positive needs a written reason of at least min_reason_chars "
                  "characters that names at least one entity in the graph, so the decision can be audited."},
    {"id": "POL-EVIDENCE-GROUNDED", "name": "Verdicts cite only graph evidence", "applies_to": "verdict",
     "rationale": "Every account or entity cited in a verdict must have been returned by a tool during the "
                  "investigation. Unverified ids are rejected (LLM planner) or removed."},
]

FAMILIES = ("money_movement", "shared_identity", "network_structure", "prior_knowledge", "other")
_FAMILY_WORDS = [
    ("network_structure", r"pagerank|betweenness|role|collector|layering mule|central|hub|community"),
    ("shared_identity", r"device|\bip\b|phone|address|identity|cluster|shared|emulator|vpn"),
    ("money_movement", r"transfer|moved|pass-through|paid|structur|loop|round-trip|merchant|cash|crypto|funds|sent|received"),
    ("prior_knowledge", r"earlier case|already linked|prior"),
]
_ID = re.compile(r"\b(?:ACC|P|DEV|ADDR|M|AL|CASE|F|ACT)-\d{3,}\b|\+91-\d{10}|\b\d{1,3}(?:\.\d{1,3}){3}\b")


def seed():
    """Write the policy set into the graph (called by load.py)."""
    rows = [{**p, "version": 1, "active": True} for p in POLICIES]
    db.write("UNWIND $rows AS r MERGE (p:Policy {id: r.id}) SET p = r", {"rows": rows})


def active(applies_to: str | None = None) -> list[dict]:
    rows = db.read("MATCH (p:Policy) RETURN p ORDER BY p.id")
    if not rows:  # fail closed: a graph loaded before policies existed gets the default set
        seed()
    rows = db.read("MATCH (p:Policy {active: true}) RETURN p ORDER BY p.id")
    out = [dict(r["p"].properties) for r in rows]
    return [p for p in out if applies_to is None or p["applies_to"] == applies_to]


def _policy(pid: str) -> dict:
    p = [x for x in active() if x["id"] == pid]
    return p[0] if p else {}


def infer_family(text: str) -> str:
    t = text.lower()
    for fam, pattern in _FAMILY_WORDS:
        if re.search(pattern, t):
            return fam
    return "other"


def new_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000) % 10_000_000:07d}{int(time.perf_counter_ns()) % 100:02d}"


def record(action_id: str, checks: list[dict]):
    """Link an Action to each policy it was evaluated against."""
    if checks:
        db.write("""
            UNWIND $checks AS c
            MATCH (x:Action {id: $xid}), (p:Policy {id: c.policy})
            CREATE (x)-[:GOVERNED_BY {result: c.result, detail: c.detail, version: p.version}]->(p)""",
                 {"xid": action_id, "checks": checks})


def overall(checks: list[dict]) -> str:
    results = {c["result"] for c in checks}
    return "blocked" if "fail" in results else "pending_analyst" if "escalated" in results else "done"


# ---------------------------------------------------------------------------
# Checks, one function per action kind
# ---------------------------------------------------------------------------

def check_freeze(case_id: str, account_ids: list[str]) -> list[dict]:
    case = db.read("""
        MATCH (c:Case {id: $id})
        OPTIONAL MATCH (c)-[:HAS_FINDING]->(f:Finding)
        RETURN c.risk AS risk, count(f) AS findings, collect(DISTINCT f.family) AS families""", {"id": case_id})[0]
    checks = []
    p = _policy("POL-FREEZE-RISK")
    if p:
        r = case["risk"]
        res = "pass" if r >= p["min_risk"] else "escalated" if r >= p["review_floor"] else "fail"
        checks.append({"policy": p["id"], "result": res,
                       "detail": f"case risk {r:.2f}; auto-freeze needs >= {p['min_risk']}, analyst review from {p['review_floor']}"})
    p = _policy("POL-FREEZE-EVIDENCE")
    if p:
        fams = sorted(f for f in case["families"] if f in ("money_movement", "shared_identity", "network_structure"))
        ok = case["findings"] >= p["min_findings"] and len(fams) >= p["min_families"]
        checks.append({"policy": p["id"], "result": "pass" if ok else "fail",
                       "detail": f"{case['findings']} findings across {len(fams)} families ({', '.join(fams) or 'none'}); "
                                 f"needs >= {p['min_findings']} findings and >= {p['min_families']} families"})
    p = _policy("POL-RESPECT-ANALYST")
    if p:
        cleared = cleared_by_analyst(account_ids)
        checks.append({"policy": p["id"], "result": "escalated" if cleared else "pass",
                       "detail": f"{len(cleared)} accounts were cleared by an analyst before: {cleared[:5]}" if cleared
                       else "no account was cleared by an analyst before"})
    p = _policy("POL-HUMAN-REVIEW")
    if p:
        n = len(account_ids)
        checks.append({"policy": p["id"], "result": "pass" if n <= p["max_auto_accounts"] else "escalated",
                       "detail": f"{n} accounts; automatic limit {p['max_auto_accounts']}"})
    return checks


def cleared_by_analyst(account_ids: list[str]) -> list[str]:
    """Accounts in an overturned case or under a freeze an analyst reversed or rejected."""
    overturned = {r["id"] for r in db.read("""
        MATCH (:Case {status: 'overturned'})-[:INCLUDES]->(a:Account) WHERE a.id IN $ids
        RETURN DISTINCT a.id AS id""", {"ids": account_ids})}
    rejected = {r["id"] for r in db.read("""
        MATCH (:Review {decision: 'reject'})-[:REVIEWS]->(:Action)-[:ON]->(a:Account) WHERE a.id IN $ids
        RETURN DISTINCT a.id AS id""", {"ids": account_ids})}
    return sorted(overturned | rejected)


def check_case(alert_id: str, account_ids: list[str]) -> tuple[list[str], list[dict]]:
    """Return the accounts allowed into the case and the policy checks."""
    p = _policy("POL-CASE-CONNECTED")
    if not p:
        return account_ids, []
    src = db.read("MATCH (:Alert {id: $id})-[:FLAGS]->(a:Account) RETURN a.id AS id", {"id": alert_id})[0]["id"]
    hops = int(p["max_hops"])
    reach = _reachable(src, set(account_ids), hops)
    kept = [a for a in account_ids if a in reach]
    dropped = [a for a in account_ids if a not in reach]
    check = {"policy": p["id"], "result": "pass" if not dropped else "fail",
             "detail": f"all {len(kept)} accounts connected to {src} within {hops} hops" if not dropped
             else f"{len(dropped)} accounts not connected to {src} within {hops} hops were left out: {dropped[:5]}"}
    return kept, [check]


def _reachable(src: str, targets: set[str], hops: int) -> set[str]:
    """Breadth-first search over identity and money edges (either direction), one hop per query,
    stopping as soon as every target is reached. Merchants are excluded: everyone shops at them."""
    seen, frontier = {src}, {src}
    for _ in range(hops):
        if targets <= seen or not frontier:
            break
        nxt = {r["id"] for r in db.read("""
            MATCH (n)-[:OWNS|USED_DEVICE|ACCESSED_FROM|HAS_PHONE|LIVES_AT|TRANSFER]-(m)
            WHERE n.id IN $ids RETURN DISTINCT m.id AS id""", {"ids": list(frontier)})}
        frontier = nxt - seen
        seen |= frontier
    return seen & (targets | {src})


def check_close(disposition: str, reason: str) -> list[dict]:
    p = _policy("POL-FP-REASON")
    if not p or disposition != "false_positive":
        return []
    reason = reason or ""
    ids = sorted(set(_ID.findall(reason)))
    found = [r["id"] for r in db.read("MATCH (n) WHERE n.id IN $ids RETURN n.id AS id", {"ids": ids})] if ids else []
    ok = len(reason) >= p["min_reason_chars"] and bool(found)
    return [{"policy": p["id"], "result": "pass" if ok else "fail",
             "detail": f"reason cites {', '.join(found[:3])}" if ok else
             f"reason must be >= {p['min_reason_chars']} characters and name at least one graph entity "
             f"(account, device, IP, phone or address id); got {len(reason)} characters, {len(found)} known ids"}]


def record_verdict(target_id: str, unverified: list[str]):
    """Write the evidence-grounding check for a finished investigation."""
    p = _policy("POL-EVIDENCE-GROUNDED")
    if not p:
        return
    xid = new_id("ACT")
    db.write("""
        MATCH (t {id: $tid})
        CREATE (x:Action {id: $xid, kind: 'verdict', status: 'done', reason: 'evidence grounding check', created: timestamp()})
        CREATE (x)-[:ON]->(t)""", {"tid": target_id, "xid": xid})
    record(xid, [{"policy": p["id"], "result": "pass" if not unverified else "fail",
                  "detail": "every cited id was returned by a tool" if not unverified
                  else f"{len(unverified)} unverified ids removed: {unverified[:5]}"}])
