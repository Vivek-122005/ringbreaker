"""Score the investigator against the generator's hidden ground truth.

    python -m ringbreaker.evaluate                 # playbook planner, every open alert
    python -m ringbreaker.evaluate --planner llm --limit 20
    python -m ringbreaker.evaluate --simulate-feedback   # before/after analyst reviews

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


async def run_pass(planner: str, limit: int = 0, verbose: bool = True) -> dict:
    """Investigate every open alert once and score the verdicts against the ground truth."""
    truth = json.load(open("data/ground_truth.json"))
    bad = {a: t for t in truth if not t["typology"].startswith("benign") for a in t["accounts"]}
    alerts = db.read("MATCH (al:Alert {status:'open'})-[:FLAGS]->(a:Account) RETURN al.id AS id, a.id AS acc ORDER BY al.id")
    m = {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "typ_ok": 0, "ring_recall": [], "done": 0, "outcomes": {},
         "verdicts": [], "ring_risks": []}
    t0 = time.time()
    for al in alerts[:limit] if limit else alerts:
        status = db.read("MATCH (al:Alert {id:$id}) RETURN al.status AS s", {"id": al["id"]})[0]["s"]
        if status != "open":  # already closed as part of an earlier ring's case
            continue
        verdict = None
        async for ev in investigate(al["id"], planner):
            if ev["type"] == "verdict":
                verdict = ev["verdict"]
            elif ev["type"] == "error" and verbose:
                print("  error", ev["message"])
        if not verdict:
            continue
        m["done"] += 1
        m["outcomes"][verdict["outcome"]] = m["outcomes"].get(verdict["outcome"], 0) + 1
        flagged = verdict["outcome"] != "false_positive"
        is_bad = al["acc"] in bad
        m["tp"] += flagged and is_bad
        m["fp"] += flagged and not is_bad
        m["tn"] += (not flagged) and not is_bad
        m["fn"] += (not flagged) and is_bad
        if is_bad:
            m["ring_risks"].append(verdict["risk"])
        if flagged and is_bad:
            ring = set(bad[al["acc"]]["accounts"])
            m["ring_recall"].append(len(ring & set(verdict["ring_accounts"])) / len(ring))
            m["typ_ok"] += verdict["typology"] == bad[al["acc"]]["typology"]
        m["verdicts"].append({"alert": al["id"], "account": al["acc"], "is_bad": is_bad, "outcome": verdict["outcome"],
                              "risk": verdict["risk"], "case_id": verdict.get("case_id"),
                              "freeze_action_id": verdict.get("freeze_action_id")})
        # close the other alerts of accounts already put in a case, as an analyst would
        if flagged:
            db.write("""MATCH (al:Alert {status:'open'})-[:FLAGS]->(a:Account) WHERE a.id IN $ids
                        SET al.status='closed', al.disposition='covered_by_case'""", {"ids": verdict["ring_accounts"]})
        if verbose:
            print(f"{al['id']} {al['acc']:>10} truth={'BAD ' if is_bad else 'ok  '} -> {verdict['outcome']:<24} "
                  f"risk={verdict['risk']:.2f} {verdict['typology']}")
    m["seconds"] = round(time.time() - t0, 1)
    frozen = db.read("MATCH (a:Account {status:'frozen'}) RETURN collect(a.id) AS ids")[0]["ids"]
    m["frozen"], m["frozen_innocent"] = len(frozen), len([a for a in frozen if a not in bad])
    m["pending_freezes"] = db.read("MATCH (x:Action {status:'pending_analyst'}) RETURN count(x) AS n")[0]["n"]
    return m


def report(m: dict):
    print(f"\ninvestigations={m['done']}  time={m['seconds']}s")
    print(f"true rings caught={m['tp']}  missed={m['fn']}  false positives cleared={m['tn']}  innocent escalated={m['fp']}")
    if m["tp"] + m["fp"]:
        print(f"precision={m['tp'] / (m['tp'] + m['fp']):.2f}  recall={m['tp'] / max(1, m['tp'] + m['fn']):.2f}  "
              f"FP alerts auto-cleared={m['tn'] / max(1, m['tn'] + m['fp']):.0%}")
    if m["ring_recall"]:
        print(f"avg share of ring members uncovered from one alert={sum(m['ring_recall']) / len(m['ring_recall']):.0%}  "
              f"typology correct={m['typ_ok']}/{m['tp']}")
    print(f"frozen accounts={m['frozen']}, of which innocent={m['frozen_innocent']}")


async def main(planner: str, limit: int, reload: bool, seed: int = 7):
    if reload:
        load(seed=seed, keep_learning=False)
    report(await run_pass(planner, limit))


async def simulate_feedback(planner: str, train_seed: int, test_seeds: list[int], review_share: float):
    """Pass 1 on a training bank; an 'analyst' (the ground truth standing in for a human) reviews a
    share of the cases; the learned tallies are carried to fresh test banks and compared with a
    no-feedback baseline on the same banks. Weights are never set from the ground truth directly:
    they move only through the same review API an analyst uses."""
    from . import learning, review
    load(seed=train_seed, keep_learning=False)
    a = await run_pass(planner, verbose=False)
    cases = [v for v in a["verdicts"] if v["case_id"]]
    step = max(1, round(1 / review_share)) if review_share > 0 else 0
    reviewed = cases[::step] if step else []
    for v in reviewed:
        review.review_case(v["case_id"], "confirm" if v["is_bad"] else "overturn", "simulated analyst")
    stats = learning.export_stats()
    calib = learning.calibration()
    print(f"training bank seed {train_seed}: {len(cases)} cases, {len(reviewed)} reviewed "
          f"({sum(v['is_bad'] for v in reviewed)} confirmed, {sum(not v['is_bad'] for v in reviewed)} overturned)")
    for x in calib["signals"]:
        if x["reviews"]:
            print(f"  {x['signal']:<26} weight {x['base_weight']:+.2f} -> {x['weight']:+.3f}  "
                  f"({x['confirmed']} confirmed / {x['overturned']} overturned)")
    for r in calib["rules"]:
        print(f"  rule {r['rule']:<20} analyst precision {r['precision']:.2f} ({r['reviews']} reviews)")
    rows = []
    for seed in test_seeds:
        load(seed=seed, keep_learning=False)
        base = await run_pass(planner, verbose=False)
        load(seed=seed, keep_learning=False)
        learning.import_stats(stats)
        fed = await run_pass(planner, verbose=False)
        rows.append((seed, base, fed))

    def line(name, f):
        print(f"  {name:<34}" + "".join(f"{f(b):>10}{f(d):>10}" for _, b, d in rows))
    print("\n  " + " " * 34 + "".join(f"{'seed ' + str(sd):>20}" for sd, _, _ in rows))
    print("  " + " " * 34 + "".join(f"{'before':>10}{'after':>10}" for _ in rows))
    line("rings caught", lambda m: m["tp"])
    line("rings missed", lambda m: m["fn"])
    line("innocent escalated", lambda m: m["fp"])
    line("false positives cleared", lambda m: m["tn"])
    line("ring_confirmed verdicts", lambda m: m["outcomes"].get("ring_confirmed", 0))
    line("suspicious_needs_review verdicts", lambda m: m["outcomes"].get("suspicious_needs_review", 0))
    line("freezes pending an analyst", lambda m: m["pending_freezes"])
    line("accounts frozen", lambda m: m["frozen"])
    line("innocent accounts frozen", lambda m: m["frozen_innocent"])
    line("lowest risk on a true ring alert", lambda m: f"{min(m['ring_risks']):.2f}" if m["ring_risks"] else "-")
    line("mean risk on true ring alerts", lambda m: f"{sum(m['ring_risks']) / len(m['ring_risks']):.2f}" if m["ring_risks"] else "-")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--planner", default="playbook", choices=["playbook", "llm", "auto"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-reload", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--simulate-feedback", action="store_true",
                    help="train on --seed with simulated analyst reviews, then compare on --test-seeds")
    ap.add_argument("--test-seeds", default="11,23,42")
    ap.add_argument("--review-share", type=float, default=0.5, help="share of cases the analyst reviews")
    a = ap.parse_args()
    if a.simulate_feedback:
        asyncio.run(simulate_feedback(a.planner, a.seed, [int(x) for x in a.test_seeds.split(",")], a.review_share))
    else:
        asyncio.run(main(a.planner, a.limit, not a.no_reload, a.seed))
