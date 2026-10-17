"""The investigator's tool catalogue.

Every tool is a fixed, reviewed Cypher query or FalkorDB graph algorithm. The
agent chooses tools and arguments; it never writes Cypher. Every id an agent
passes in is resolved against the graph before a query runs, which blocks
made-up entities and injection.

Each tool returns a dict with:
  data      what the tool found (JSON-safe)
  evidence  {"nodes": [...ids], "edges": [[src, rel, dst], ...]} for the UI and
            for answer validation (an agent may only cite ids that appeared here)
"""
from __future__ import annotations

import time
from typing import Any

from . import db

MAX_HOPS = 5
LABELS = ["Account", "Person", "Device", "IP", "Phone", "Address", "Merchant", "Alert", "Case", "Finding"]


class ToolError(ValueError):
    pass


def _ev(nodes=(), edges=()):
    return {"nodes": sorted(set(nodes)), "edges": [list(e) for e in edges]}


def _resolve(entity_id: str, label: str | None = None) -> str:
    """Return the label of an existing entity or raise."""
    if not isinstance(entity_id, str) or not entity_id or len(entity_id) > 64:
        raise ToolError(f"invalid id {entity_id!r}")
    labels = [label] if label else LABELS
    for lb in labels:
        if db.read(f"MATCH (n:{lb} {{id: $id}}) RETURN n.id AS id LIMIT 1", {"id": entity_id}):
            return lb
    raise ToolError(f"{label or 'entity'} {entity_id} does not exist in the graph")


def _accounts(ids: list[str]) -> list[str]:
    if not ids:
        raise ToolError("no account ids given")
    found = {r["id"] for r in db.read("MATCH (a:Account) WHERE a.id IN $ids RETURN a.id AS id", {"ids": list(ids)})}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ToolError(f"unknown accounts: {missing[:5]}")
    return list(dict.fromkeys(ids))


def _days(ts_a: int, ts_b: int) -> int:
    return int((ts_a - ts_b) // 86_400)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def list_open_alerts(limit: int = 50) -> dict:
    """Open transaction-monitoring alerts, newest rule hits first."""
    r = db.read("""
        MATCH (al:Alert {status: 'open'})-[:FLAGS]->(a:Account)
        RETURN al.id AS alert, al.rule AS rule, al.detail AS detail, a.id AS account
        ORDER BY al.id LIMIT $limit""", {"limit": int(limit)})
    return {"data": r, "evidence": _ev([x["account"] for x in r])}


def get_alert(alert_id: str) -> dict:
    """The alert, the account it flags and every other open alert on that account."""
    _resolve(alert_id, "Alert")
    r = db.read("""
        MATCH (al:Alert {id: $id})-[:FLAGS]->(a:Account)
        OPTIONAL MATCH (other:Alert)-[:FLAGS]->(a) WHERE other.id <> al.id
        RETURN al.id AS alert, al.rule AS rule, al.description AS description, al.detail AS detail,
               al.status AS status, a.id AS account,
               collect(DISTINCT {id: other.id, rule: other.rule, detail: other.detail}) AS other_alerts""",
                {"id": alert_id})[0]
    r["other_alerts"] = [o for o in r["other_alerts"] if o.get("id")]
    return {"data": r, "evidence": _ev([r["account"]])}


def account_profile(account_id: str) -> dict:
    """KYC and behaviour summary: owner, account age, balance, money in/out, counterparties, merchants."""
    _resolve(account_id, "Account")
    r = db.read("""
        MATCH (p:Person)-[:OWNS]->(a:Account {id: $id})
        OPTIONAL MATCH (a)<-[i:TRANSFER]-(src:Account)
        WITH p, a, sum(i.amount) AS inflow, count(DISTINCT src) AS senders, max(i.ts) AS last_in
        OPTIONAL MATCH (a)-[o:TRANSFER]->(dst:Account)
        WITH p, a, inflow, senders, last_in, sum(o.amount) AS outflow, count(DISTINCT dst) AS receivers, max(o.ts) AS last_out
        OPTIONAL MATCH (a)-[pay:PAID]->(m:Merchant)
        WITH p, a, inflow, senders, last_in, outflow, receivers, last_out, m, sum(pay.amount) AS spent
        RETURN p.id AS owner, p.name AS owner_name, p.segment AS segment, p.kyc_date AS kyc_date,
               a.type AS type, a.opened AS opened, a.status AS status, a.balance AS balance,
               round(inflow) AS total_in, senders, round(outflow) AS total_out, receivers,
               last_in, last_out,
               collect(CASE WHEN m IS NULL THEN NULL ELSE {merchant: m.name, category: m.category,
                                                         risk: m.risk, spent: round(spent)} END) AS merchants""",
                {"id": account_id})[0]
    latest = max(x for x in (r.pop("last_in"), r.pop("last_out"), r["opened"]) if x)
    r["account_age_days_at_last_activity"] = _days(latest, r.pop("opened"))
    r["customer_since_days"] = _days(latest, r.pop("kyc_date"))
    r["merchants"] = sorted(r["merchants"], key=lambda m: -m["spent"])[:6]
    r["pass_through_ratio"] = round(r["total_out"] / r["total_in"], 2) if r["total_in"] else None
    return {"data": r, "evidence": _ev([account_id, r["owner"]], [[r["owner"], "OWNS", account_id]])}


def follow_money(account_id: str, direction: str = "out", max_hops: int = 3, window_hours: int = 72,
                 min_amount: float = 1000) -> dict:
    """Trace funds hop by hop where each transfer happens after the previous one and within
    `window_hours` of it (how laundered money actually moves). direction 'out' follows where money
    went, 'in' traces where it came from."""
    _resolve(account_id, "Account")
    hops = max(1, min(int(max_hops), MAX_HOPS))
    window = max(1, min(int(window_hours), 24 * 30)) * 3600
    if direction not in ("out", "in"):
        raise ToolError("direction must be 'out' or 'in'")
    pattern = f"(a:Account {{id: $id}})-[:TRANSFER*1..{hops}]->(x:Account)" if direction == "out" \
        else f"(x:Account)-[:TRANSFER*1..{hops}]->(a:Account {{id: $id}})"
    r = db.read(f"""
        MATCH p = {pattern}
        WITH p, relationships(p) AS r
        WHERE all(e IN r WHERE e.amount >= $min)
          AND all(i IN range(0, size(r) - 2) WHERE r[i+1].ts >= r[i].ts AND r[i+1].ts - r[i].ts <= $window)
        RETURN [n IN nodes(p) | n.id] AS path,
               [e IN r | {{from: startNode(e).id, to: endNode(e).id, amount: e.amount, ts: e.ts, channel: e.channel}}] AS hops
        LIMIT 400""", {"id": account_id, "min": float(min_amount), "window": window})
    # merge parallel transfers along the same account path into one flow per path
    # (each distinct transfer is counted once per hop, even when it appears in several combinations)
    groups: dict[tuple, list] = {}
    for x in r:
        groups.setdefault(tuple(x["path"]), []).append(x["hops"])
    merged = {}
    for key, combos in groups.items():
        hops = []
        for i in range(len(key) - 1):
            distinct = {(h["ts"], h["amount"]): h for c in combos for h in [c[i]]}.values()
            hops.append({"from": key[i], "to": key[i + 1], "amount": round(sum(h["amount"] for h in distinct), 2),
                         "transfers": len(distinct), "first_ts": min(h["ts"] for h in distinct),
                         "channel": next(iter(distinct))["channel"]})
        merged[key] = {"path": list(key), "hops": hops}
    # keep only maximal paths (drop prefixes) and rank by money moved
    paths = sorted(merged.values(), key=lambda x: -len(x["path"]))
    kept, seen_prefix = [], set()
    for p in paths:
        key = tuple(p["path"])
        if key in seen_prefix:
            continue
        for k in range(2, len(key)):
            seen_prefix.add(key[:k] if direction == "out" else key[-k:])
        kept.append(p)
    kept.sort(key=lambda p: -sum(h["amount"] for h in p["hops"]))
    kept = kept[:15]
    nodes, edges = set(), []
    for p in kept:
        nodes.update(p["path"])
        edges += [[h["from"], "TRANSFER", h["to"]] for h in p["hops"]]
    endpoints = {}
    for p in kept:
        end = p["path"][-1] if direction == "out" else p["path"][0]
        endpoints[end] = endpoints.get(end, 0) + p["hops"][-1 if direction == "out" else 0]["amount"]
    data = {"paths": kept, "distinct_accounts_reached": len(nodes) - 1,
            "endpoints": [{"account": k, "amount": round(v)} for k, v in sorted(endpoints.items(), key=lambda kv: -kv[1])]}
    if not kept:
        data["note"] = "No time-ordered flow found. An empty trace means the graph has no such movement, not that the account is clean."
    return {"data": data, "evidence": _ev(nodes, edges)}


def find_cycles(account_id: str, max_length: int = 6) -> dict:
    """Money loops that leave and return to this account (round-tripping)."""
    _resolve(account_id, "Account")
    n = max(2, min(int(max_length), 7))
    r = db.read(f"""
        MATCH p = (a:Account {{id: $id}})-[:TRANSFER*2..{n}]->(a)
        WITH p, relationships(p) AS r
        RETURN [e IN r | {{from: startNode(e).id, to: endNode(e).id, amount: e.amount, ts: e.ts}}] AS hops
        LIMIT 50""", {"id": account_id})
    cycles = []
    for c in r:
        # FalkorDB may return a cycle's edges in reverse order; rebuild it starting at the account
        hops = sorted(c["hops"], key=lambda h: h["ts"])
        amounts = [h["amount"] for h in hops]
        cycles.append({"accounts": [h["from"] for h in hops] + [hops[-1]["to"]], "hops": hops,
                       "value_retained_pct": round(100 * min(amounts) / max(amounts), 1),
                       "duration_days": round((hops[-1]["ts"] - hops[0]["ts"]) / 86400, 1)})
    uniq = {}
    for c in cycles:
        uniq.setdefault(tuple(sorted(set(c["accounts"]))), c)
    cycles = list(uniq.values())[:10]
    nodes = {a for c in cycles for a in c["accounts"]}
    edges = [[h["from"], "TRANSFER", h["to"]] for c in cycles for h in c["hops"]]
    return {"data": {"cycles": cycles, "count": len(cycles)}, "evidence": _ev(nodes or [account_id], edges)}


def shared_identifiers(account_id: str) -> dict:
    """Other customers linked to this account through the same device, IP, phone or address,
    with how common each identifier is (a home router shared by a family is normal; an emulator
    shared by 9 strangers is not)."""
    _resolve(account_id, "Account")
    r = db.read("""
        MATCH (p:Person)-[:OWNS]->(a:Account {id: $id})
        CALL {
          WITH a MATCH (a)-[:USED_DEVICE]->(x:Device)<-[:USED_DEVICE]-(b:Account)
          RETURN 'Device' AS kind, x.id AS identifier, x.os AS info, b
          UNION
          WITH a MATCH (a)-[:ACCESSED_FROM]->(x:IP)<-[:ACCESSED_FROM]-(b:Account)
          RETURN 'IP' AS kind, x.id AS identifier, x.asn + ' / ' + x.kind AS info, b
          UNION
          WITH p MATCH (p)-[:HAS_PHONE]->(x:Phone)<-[:HAS_PHONE]-(:Person)-[:OWNS]->(b:Account)
          RETURN 'Phone' AS kind, x.id AS identifier, '' AS info, b
          UNION
          WITH p MATCH (p)-[:LIVES_AT]->(x:Address)<-[:LIVES_AT]-(:Person)-[:OWNS]->(b:Account)
          RETURN 'Address' AS kind, x.id AS identifier, x.line + ', ' + x.city AS info, b
        }
        WITH kind, identifier, info, b WHERE b.id <> $id
        MATCH (q:Person)-[:OWNS]->(b)
        RETURN kind, identifier, info, collect(DISTINCT {account: b.id, owner: q.name, opened: b.opened}) AS linked""",
                {"id": account_id})
    nodes, edges = {account_id}, []
    rel = {"Device": "USED_DEVICE", "IP": "ACCESSED_FROM", "Phone": "HAS_PHONE", "Address": "LIVES_AT"}
    surnames = {}
    for row in r:
        row["linked_count"] = len(row["linked"])
        row["linked"] = row["linked"][:12]
        nodes.add(row["identifier"])
        edges.append([account_id, rel[row["kind"]], row["identifier"]])
        for l in row["linked"]:
            nodes.add(l["account"])
            edges.append([l["account"], rel[row["kind"]], row["identifier"]])
            surnames.setdefault(row["identifier"], set()).add(l["owner"].split()[-1])
        row["distinct_surnames"] = len(surnames.get(row["identifier"], ()))
    return {"data": {"shared": r, "count": len(r)}, "evidence": _ev(nodes, edges)}


def identity_cluster(account_id: str) -> dict:
    """Run Weakly Connected Components over the identity graph (people, accounts, devices,
    phones, addresses) and return the cluster this account belongs to: every account that is
    controlled through a chain of shared identity elements."""
    _resolve(account_id, "Account")
    comp = db.read("""
        CALL algo.WCC({nodeLabels: ['Account', 'Person', 'Device', 'Phone', 'Address'],
                       relationshipTypes: ['OWNS', 'USED_DEVICE', 'HAS_PHONE', 'LIVES_AT']})
        YIELD node, componentId
        WITH node, componentId WHERE node.id = $id
        RETURN componentId""", {"id": account_id})
    if not comp:
        return {"data": {"accounts": [], "note": "account not in identity graph"}, "evidence": _ev([account_id])}
    cid = comp[0]["componentId"]
    members = db.read("""
        CALL algo.WCC({nodeLabels: ['Account', 'Person', 'Device', 'Phone', 'Address'],
                       relationshipTypes: ['OWNS', 'USED_DEVICE', 'HAS_PHONE', 'LIVES_AT']})
        YIELD node, componentId
        WITH node WHERE componentId = $cid AND node:Account
        MATCH (p:Person)-[:OWNS]->(node)
        RETURN node.id AS account, p.name AS owner, node.opened AS opened, node.status AS status
        ORDER BY node.opened DESC LIMIT 60""", {"cid": cid})
    opened = [m["opened"] for m in members]
    data = {"cluster_size": len(members), "accounts": members,
            "opened_within_days": _days(max(opened), min(opened)) if opened else None,
            "distinct_owner_surnames": len({m["owner"] if m["owner"].endswith("Ltd") else m["owner"].split()[-1] for m in members})}
    return {"data": data, "evidence": _ev([m["account"] for m in members])}


def rank_roles(account_ids: list[str]) -> dict:
    """Score accounts in a suspected ring with PageRank and betweenness centrality over the
    transfer graph, plus in/out counts inside the ring, to tell collectors, layering mules and
    cash-out points apart."""
    ids = _accounts(account_ids[:60])
    pr = {r["id"]: r["score"] for r in db.read("""
        CALL algo.pageRank('Account', 'TRANSFER') YIELD node, score
        WITH node, score WHERE node.id IN $ids RETURN node.id AS id, score""", {"ids": ids})}
    bc = {r["id"]: r["score"] for r in db.read("""
        CALL algo.betweenness({nodeLabels: ['Account'], relationshipTypes: ['TRANSFER']}) YIELD node, score
        WITH node, score WHERE node.id IN $ids RETURN node.id AS id, score""", {"ids": ids})}
    flows = db.read("""
        MATCH (a:Account)-[t:TRANSFER]->(b:Account) WHERE a.id IN $ids AND b.id IN $ids
        RETURN a.id AS src, b.id AS dst, sum(t.amount) AS amount, count(t) AS n""", {"ids": ids})
    ext_in = {r["id"]: r for r in db.read("""
        MATCH (s:Account)-[t:TRANSFER]->(a:Account) WHERE a.id IN $ids AND NOT s.id IN $ids
        RETURN a.id AS id, count(DISTINCT s) AS senders, sum(t.amount) AS amount""", {"ids": ids})}
    cash = {r["id"]: r for r in db.read("""
        MATCH (a:Account)-[p:PAID]->(m:Merchant) WHERE a.id IN $ids AND m.risk >= 0.6
        RETURN a.id AS id, sum(p.amount) AS amount, collect(DISTINCT m.name) AS merchants,
               collect(DISTINCT m.id) AS merchant_ids""", {"ids": ids})}
    out = []
    for i in ids:
        fin = sum(f["amount"] for f in flows if f["dst"] == i)
        fout = sum(f["amount"] for f in flows if f["src"] == i)
        e = ext_in.get(i, {})
        c = cash.get(i, {})
        if e.get("senders", 0) >= 3 and fout > 0:
            role = "collector"
        elif c.get("amount", 0) > 0 and fin > 0:
            role = "cash_out"
        elif fin > 0 and fout > 0:
            role = "layering"
        elif fout > 0:
            role = "source"
        elif fin > 0:
            role = "sink"
        else:
            role = "unconnected"
        out.append({"account": i, "role": role, "pagerank": round(pr.get(i, 0), 5),
                    "betweenness": round(bc.get(i, 0), 1), "in_ring_in": round(fin), "in_ring_out": round(fout),
                    "external_senders": e.get("senders", 0), "external_in": round(e.get("amount", 0)),
                    "high_risk_spend": round(c.get("amount", 0)), "high_risk_merchants": c.get("merchants", [])})
    out.sort(key=lambda x: -x["betweenness"])
    edges = [[f["src"], "TRANSFER", f["dst"]] for f in flows]
    merchant_ids = [m for c in cash.values() for m in c["merchant_ids"]]
    edges += [[a, "PAID", m] for a, c in cash.items() for m in c["merchant_ids"]]
    return {"data": {"accounts": out, "internal_flows": len(flows),
                     "money_in_from_outside": round(sum(x["external_in"] for x in out)),
                     "money_to_high_risk_merchants": round(sum(x["high_risk_spend"] for x in out))},
            "evidence": _ev(ids + merchant_ids, edges)}


def explain_connection(from_id: str, to_id: str) -> dict:
    """Shortest path (algo.SPpaths) between any two entities over every relationship type,
    in either direction: answers 'how are these two connected?'"""
    _resolve(from_id)
    _resolve(to_id)
    r = db.read("""
        MATCH (a {id: $a}), (b {id: $b})
        CALL algo.SPpaths({sourceNode: a, targetNode: b, relDirection: 'both', maxLen: 6, pathCount: 3})
        YIELD path
        RETURN [n IN nodes(path) | n.id] AS nodes,
               [e IN relationships(path) | [startNode(e).id, type(e), endNode(e).id]] AS edges""",
                {"a": from_id, "b": to_id})
    if not r:
        return {"data": {"connected": False, "note": "No path within 6 hops."}, "evidence": _ev([from_id, to_id])}
    r = list({tuple(p["nodes"]): p for p in r}.values())
    nodes = {n for p in r for n in p["nodes"]}
    edges = [e for p in r for e in p["edges"]]
    return {"data": {"connected": True, "paths": r}, "evidence": _ev(nodes, edges)}


def prior_knowledge(entity_ids: list[str]) -> dict:
    """Findings, cases and actions already recorded on these entities by earlier investigations."""
    ids = list(dict.fromkeys(entity_ids))[:60]
    r = db.read("""
        MATCH (n) WHERE n.id IN $ids
        OPTIONAL MATCH (f:Finding)-[:ABOUT]->(n)
        OPTIONAL MATCH (c:Case)-[:INCLUDES]->(n)
        WITH n, collect(DISTINCT {finding: f.id, text: f.text, created: f.created}) AS fs,
             collect(DISTINCT {case: c.id, typology: c.typology, status: c.status, risk: c.risk}) AS cs
        RETURN n.id AS entity, n.status AS status,
               [x IN fs WHERE x.finding IS NOT NULL] AS findings, [x IN cs WHERE x.case IS NOT NULL] AS cases""",
                {"ids": ids})
    r = [x for x in r if x["findings"] or x["cases"] or x["status"] == "frozen"]
    return {"data": {"known": r, "count": len(r)} if r else {"known": [], "note": "Nothing recorded yet on these entities."},
            "evidence": _ev([x["entity"] for x in r])}


# ---------------------------------------------------------------------------
# Action tools (write to the graph)
# ---------------------------------------------------------------------------

def open_case(alert_id: str, account_ids: list[str], typology: str, risk: float, summary: str) -> dict:
    """Open an investigation case linking the alert and every account in the ring."""
    _resolve(alert_id, "Alert")
    ids = _accounts(account_ids)
    cid = f"CASE-{int(time.time() * 1000) % 10_000_000:07d}"
    db.write("""
        MATCH (al:Alert {id: $alert})
        CREATE (c:Case {id: $cid, typology: $typ, risk: $risk, summary: $summary, status: 'open',
                        created: timestamp()})-[:RAISED_FROM]->(al)
        WITH c
        UNWIND $ids AS aid MATCH (a:Account {id: aid}) CREATE (c)-[:INCLUDES]->(a)""",
             {"alert": alert_id, "cid": cid, "typ": str(typology)[:60], "risk": float(risk),
              "summary": str(summary)[:2000], "ids": ids})
    return {"data": {"case": cid, "accounts": len(ids)}, "evidence": _ev([cid, alert_id] + ids)}


def record_finding(case_id: str, entity_ids: list[str], text: str) -> dict:
    """Write a finding into the graph so future investigations start from it."""
    _resolve(case_id, "Case")
    ids = list(dict.fromkeys(entity_ids))[:40]
    for i in ids:
        _resolve(i)
    fid = f"F-{int(time.time() * 1000) % 10_000_000:07d}"
    db.write("""
        MATCH (c:Case {id: $cid})
        CREATE (c)-[:HAS_FINDING]->(f:Finding {id: $fid, text: $text, created: timestamp()})
        WITH f UNWIND $ids AS eid MATCH (n {id: eid}) CREATE (f)-[:ABOUT]->(n)""",
             {"cid": case_id, "fid": fid, "text": str(text)[:1000], "ids": ids})
    return {"data": {"finding": fid}, "evidence": _ev([fid] + ids)}


def freeze_accounts(case_id: str, account_ids: list[str], reason: str) -> dict:
    """Freeze outgoing payments on accounts in a case. Only allowed for cases with risk >= 0.8
    (bank policy); otherwise the request is recorded as a recommendation for an analyst."""
    _resolve(case_id, "Case")
    ids = _accounts(account_ids)
    risk = db.read("MATCH (c:Case {id: $id}) RETURN c.risk AS r", {"id": case_id})[0]["r"]
    if risk < 0.8:
        status, kind = "pending_analyst", "freeze_recommended"
    else:
        status, kind = "done", "freeze"
        db.write("UNWIND $ids AS aid MATCH (a:Account {id: aid}) SET a.status = 'frozen'", {"ids": ids})
    db.write("""
        MATCH (c:Case {id: $cid})
        CREATE (c)-[:TOOK]->(x:Action {id: $xid, kind: $kind, status: $status, reason: $reason, created: timestamp()})
        WITH x UNWIND $ids AS aid MATCH (a:Account {id: aid}) CREATE (x)-[:ON]->(a)""",
             {"cid": case_id, "xid": f"ACT-{int(time.time() * 1000) % 10_000_000:07d}", "kind": kind,
              "status": status, "reason": str(reason)[:500], "ids": ids})
    return {"data": {"action": kind, "status": status, "accounts": ids, "case_risk": risk},
            "evidence": _ev(ids + [case_id])}


def close_alert(alert_id: str, disposition: str, reason: str) -> dict:
    """Close an alert as 'false_positive' or 'escalated_to_case' with a documented reason (audit trail)."""
    _resolve(alert_id, "Alert")
    if disposition not in ("false_positive", "escalated_to_case"):
        raise ToolError("disposition must be false_positive or escalated_to_case")
    db.write("MATCH (al:Alert {id: $id}) SET al.status = 'closed', al.disposition = $d, al.reason = $r, al.closed = timestamp()",
             {"id": alert_id, "d": disposition, "r": str(reason)[:1000]})
    return {"data": {"alert": alert_id, "disposition": disposition}, "evidence": _ev([alert_id])}


READ_TOOLS = [get_alert, account_profile, follow_money, find_cycles, shared_identifiers, identity_cluster,
              rank_roles, explain_connection, prior_knowledge]
ACTION_TOOLS = [open_case, record_finding, freeze_accounts, close_alert]
ALL_TOOLS: dict[str, Any] = {f.__name__: f for f in READ_TOOLS + ACTION_TOOLS + [list_open_alerts]}
