"""Score the investigator against the generator's hidden ground truth.

    python -m ringbreaker.evaluate                 # playbook planner, every open alert
    python -m ringbreaker.evaluate --planner llm --limit 20

Reloads a fresh dataset first so actions from earlier runs do not leak in.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

from . import db
from .agent import investigate
from .load import load


async def main(planner: str, limit: int, reload: bool, seed: int = 7):
    if reload:
        load(seed=seed)
    truth = json.load(open("data/ground_truth.json"))
    bad = {a: t for t in truth if not t["typology"].startswith("benign") for a in t["accounts"]}
    alerts = db.read("MATCH (al:Alert {status:'open'})-[:FLAGS]->(a:Account) RETURN al.id AS id, a.id AS acc ORDER BY al.id")
    tp = fp = tn = fn = 0
    typ_ok = 0
    ring_recall = []
    t0 = time.time()
    done = 0
    for al in alerts[:limit] if limit else alerts:
        status = db.read("MATCH (al:Alert {id:$id}) RETURN al.status AS s", {"id": al["id"]})[0]["s"]
        if status != "open":  # already closed as part of an earlier ring's case
            continue
        verdict = None
        async for ev in investigate(al["id"], planner):
            if ev["type"] == "verdict":
                verdict = ev["verdict"]
            elif ev["type"] == "error":
                print("  error", ev["message"])
        if not verdict:
            continue
        done += 1
        flagged = verdict["outcome"] != "false_positive"
        is_bad = al["acc"] in bad
        tp += flagged and is_bad
        fp += flagged and not is_bad
        tn += (not flagged) and not is_bad
        fn += (not flagged) and is_bad
        if flagged and is_bad:
            ring = set(bad[al["acc"]]["accounts"])
            ring_recall.append(len(ring & set(verdict["ring_accounts"])) / len(ring))
            typ_ok += verdict["typology"] == bad[al["acc"]]["typology"]
        # close the other alerts of accounts already put in a case, as an analyst would
        if flagged:
            db.write("""MATCH (al:Alert {status:'open'})-[:FLAGS]->(a:Account) WHERE a.id IN $ids
                        SET al.status='closed', al.disposition='covered_by_case'""", {"ids": verdict["ring_accounts"]})
        print(f"{al['id']} {al['acc']:>10} truth={'BAD ' if is_bad else 'ok  '} -> {verdict['outcome']:<24} "
              f"risk={verdict['risk']:.2f} {verdict['typology']}")
    print(f"\ninvestigations={done}  time={time.time() - t0:.1f}s")
    print(f"true rings caught={tp}  missed={fn}  false positives cleared={tn}  innocent escalated={fp}")
    if tp + fp:
        print(f"precision={tp / (tp + fp):.2f}  recall={tp / max(1, tp + fn):.2f}  "
              f"FP alerts auto-cleared={tn / max(1, tn + fp):.0%}")
    if ring_recall:
        print(f"avg share of ring members uncovered from one alert={sum(ring_recall) / len(ring_recall):.0%}  "
              f"typology correct={typ_ok}/{tp}")
    frozen = db.read("MATCH (a:Account {status:'frozen'}) RETURN collect(a.id) AS ids")[0]["ids"]
    print(f"frozen accounts={len(frozen)}, of which innocent={len([a for a in frozen if a not in bad])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--planner", default="playbook", choices=["playbook", "llm", "auto"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    asyncio.run(main(a.planner, a.limit, not a.no_reload, a.seed))
