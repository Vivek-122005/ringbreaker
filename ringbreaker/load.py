"""Generate the synthetic bank, load it into FalkorDB and run transaction monitoring.

    python -m ringbreaker.load            # default seed, ~2.4k customers
    python -m ringbreaker.load --people 800
"""
from __future__ import annotations

import argparse
import os
import time

from . import db, monitoring
from .generator import generate, save_truth

INDEXES = [
    "CREATE INDEX FOR (n:Account) ON (n.id)",
    "CREATE INDEX FOR (n:Person) ON (n.id)",
    "CREATE INDEX FOR (n:Device) ON (n.id)",
    "CREATE INDEX FOR (n:IP) ON (n.id)",
    "CREATE INDEX FOR (n:Phone) ON (n.id)",
    "CREATE INDEX FOR (n:Address) ON (n.id)",
    "CREATE INDEX FOR (n:Merchant) ON (n.id)",
    "CREATE INDEX FOR (n:Alert) ON (n.id)",
    "CREATE INDEX FOR (n:Case) ON (n.id)",
    "CREATE INDEX FOR (n:Finding) ON (n.id)",
]


def _batches(items, size=2000):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def load(seed: int = 7, people: int = 2400, truth_path: str = "data/ground_truth.json"):
    t = time.time()
    w = generate(seed, people)
    g = db.graph()
    try:
        g.delete()
    except Exception:
        pass
    g = db.graph()
    for stmt in INDEXES:
        g.query(stmt)

    def nodes(label, items):
        for b in _batches(list(items)):
            g.query(f"UNWIND $rows AS r CREATE (n:{label}) SET n = r", {"rows": b})

    nodes("Person", w.persons)
    nodes("Account", w.accounts)
    nodes("Phone", w.phones.values())
    nodes("Address", w.addresses.values())
    nodes("Device", w.devices.values())
    nodes("IP", w.ips.values())
    nodes("Merchant", w.merchants)

    def edges(a_label, b_label, rel, pairs):
        for b in _batches([{"a": a, "b": c} for a, c in pairs]):
            g.query(f"UNWIND $rows AS r MATCH (a:{a_label} {{id: r.a}}), (b:{b_label} {{id: r.b}}) "
                    f"CREATE (a)-[:{rel}]->(b)", {"rows": b})

    edges("Person", "Account", "OWNS", w.owns)
    edges("Person", "Phone", "HAS_PHONE", w.has_phone)
    edges("Person", "Address", "LIVES_AT", w.lives_at)
    edges("Account", "Device", "USED_DEVICE", set(w.used_device))
    edges("Account", "IP", "ACCESSED_FROM", set(w.accessed_from))

    for b in _batches(w.transfers):
        g.query("UNWIND $rows AS r MATCH (a:Account {id: r.src}), (b:Account {id: r.dst}) "
                "CREATE (a)-[:TRANSFER {id: r.id, amount: r.amount, ts: r.ts, channel: r.channel}]->(b)",
                {"rows": b})
    for b in _batches(w.payments):
        g.query("UNWIND $rows AS r MATCH (a:Account {id: r.src}), (m:Merchant {id: r.dst}) "
                "CREATE (a)-[:PAID {amount: r.amount, ts: r.ts}]->(m)", {"rows": b})

    os.makedirs(os.path.dirname(truth_path) or ".", exist_ok=True)
    save_truth(w, truth_path)
    n_alerts = monitoring.run()
    stats = db.read("MATCH (n) RETURN count(n) AS nodes")[0]["nodes"], db.read("MATCH ()-[r]->() RETURN count(r) AS e")[0]["e"]
    print(f"loaded {stats[0]} nodes, {stats[1]} edges, {n_alerts} alerts in {time.time() - t:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--people", type=int, default=2400)
    a = ap.parse_args()
    os.makedirs("data", exist_ok=True)
    load(a.seed, a.people)
