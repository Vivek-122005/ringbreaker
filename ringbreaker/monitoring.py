"""Rule-based transaction monitoring, the way most banks generate alerts today.

These rules look at one account at a time. They are noisy on purpose: the
point of RingBreaker is that the graph investigation separates real rings
from benign look-alikes that trip the same rules.
"""
from __future__ import annotations

from . import db

DAY = 86_400

RULES = {
    "RAPID_MOVEMENT": (
        "Funds received and >=80% sent out again within 24h",
        """
        MATCH (s:Account)-[i:TRANSFER]->(a:Account)-[o:TRANSFER]->(d:Account)
        WHERE o.ts >= i.ts AND o.ts - i.ts <= 86400
        WITH a, sum(DISTINCT i.amount) AS inflow, sum(DISTINCT o.amount) AS outflow
        WHERE inflow >= 20000 AND outflow >= 0.8 * inflow
        RETURN a.id AS account, toInteger(round(inflow)) AS inflow, toInteger(round(outflow)) AS outflow
        """,
    ),
    "STRUCTURING": (
        "3+ transfers between 9,000 and 9,999 in the window",
        """
        MATCH (a:Account)-[t:TRANSFER]->()
        WHERE t.amount >= 9000 AND t.amount < 10000
        WITH a, count(t) AS n WHERE n >= 3
        RETURN a.id AS account, n
        """,
    ),
    "FAN_IN": (
        "8+ distinct senders",
        """
        MATCH (s:Account)-[:TRANSFER]->(a:Account)
        WITH a, count(DISTINCT s) AS senders WHERE senders >= 8
        RETURN a.id AS account, senders
        """,
    ),
    "SHARED_DEVICE": (
        "Device used by accounts of 3+ different customers",
        """
        MATCH (p:Person)-[:OWNS]->(a:Account)-[:USED_DEVICE]->(d:Device)
        WITH d, collect(DISTINCT p) AS owners, collect(DISTINCT a) AS accts
        WHERE size(owners) >= 3
        UNWIND accts AS a
        RETURN a.id AS account, d.id AS device, size(owners) AS owners
        """,
    ),
    "HIGH_RISK_MERCHANT": (
        "Over 50,000 spent at high-risk merchants (crypto / gift cards / remittance)",
        """
        MATCH (a:Account)-[p:PAID]->(m:Merchant) WHERE m.risk >= 0.6
        WITH a, sum(p.amount) AS spent WHERE spent > 50000
        RETURN a.id AS account, toInteger(round(spent)) AS spent
        """,
    ),
}


def run() -> int:
    """Evaluate every rule and write one open Alert per (account, rule)."""
    created = 0
    db.write("MATCH (al:Alert) DETACH DELETE al")
    seq = 0
    for rule, (desc, cypher) in RULES.items():
        rows = []
        for row in db.read(cypher):
            seq += 1
            rows.append({"acc": row["account"], "id": f"AL-{seq:05d}",
                         "detail": ", ".join(f"{k}={v}" for k, v in row.items() if k != "account")})
        if rows:
            db.write(
                """UNWIND $rows AS r
                   MATCH (a:Account {id: r.acc})
                   CREATE (al:Alert {id: r.id, rule: $rule, description: $desc, detail: r.detail,
                                     status: 'open', created: timestamp()})-[:FLAGS]->(a)""",
                {"rows": rows, "rule": rule, "desc": desc},
            )
        created += len(rows)
    return created
