"""Transparent learning from analyst reviews.

Every playbook evidence signal and every monitoring rule keeps a running tally of how often
analysts confirmed or overturned the cases it contributed to, stored in the graph as
(:SignalStat {signal, confirmed, overturned}) and (:RuleStat {rule, confirmed, overturned}).

A signal's weight is adjusted by a bounded, smoothed factor:

    precision = (confirmed + PSEUDO * PRIOR) / (confirmed + overturned + PSEUDO)
    factor    = clamp(precision / PRIOR, MIN_FACTOR, MAX_FACTOR)
    weight    = base_weight * factor

PRIOR is the precision a signal is assumed to have before any review, PSEUDO is how many
reviews that assumption is worth (Laplace-style smoothing, so a single review moves a weight
only a little), and the clamp keeps every weight within +/-50% of its reviewed base value.
Only positive (risk-raising) weights are calibrated; benign signals keep their base weight.
"""
from __future__ import annotations

from . import db

PRIOR = 0.75
PSEUDO = 4.0
MIN_FACTOR, MAX_FACTOR = 0.5, 1.5

# Base weights of the playbook's evidence signals (see playbook.py).
SIGNALS = {
    "pass_through_new": 0.20,
    "pass_through_old": 0.05,
    "long_standing_customer": -0.15,
    "prior_case": 0.30,
    "analyst_cleared": -0.30,
    "risky_infra_shared": 0.25,
    "device_shared_unrelated": 0.20,
    "identity_shared_unrelated": 0.15,
    "household_only": -0.10,
    "identity_cluster": 0.20,
    "structuring": 0.15,
    "fast_flow_linked": 0.15,
    "many_new_payers": 0.15,
    "round_trip_loop": 0.60,
    "high_risk_cash_out": 0.20,
}


def factor(confirmed: int, overturned: int) -> float:
    precision = (confirmed + PSEUDO * PRIOR) / (confirmed + overturned + PSEUDO)
    return max(MIN_FACTOR, min(MAX_FACTOR, precision / PRIOR))


def calibration() -> dict:
    """Current weight of every signal and precision of every rule, with the counts behind them."""
    stats = {r["signal"]: r for r in db.read(
        "MATCH (s:SignalStat) RETURN s.signal AS signal, s.confirmed AS confirmed, s.overturned AS overturned")}
    signals = []
    for name, base in SIGNALS.items():
        s = stats.get(name, {"confirmed": 0, "overturned": 0})
        f = factor(s["confirmed"], s["overturned"]) if base > 0 else 1.0
        signals.append({"signal": name, "base_weight": base, "weight": round(base * f, 3), "factor": round(f, 3),
                        "confirmed": s["confirmed"], "overturned": s["overturned"],
                        "reviews": s["confirmed"] + s["overturned"]})
    rules = [{**r, "reviews": r["confirmed"] + r["overturned"],
              "precision": round(r["confirmed"] / max(1, r["confirmed"] + r["overturned"]), 3)}
             for r in db.read("MATCH (s:RuleStat) RETURN s.rule AS rule, s.confirmed AS confirmed, "
                              "s.overturned AS overturned ORDER BY s.rule")]
    return {"signals": signals, "rules": rules,
            "method": {"prior_precision": PRIOR, "pseudo_reviews": PSEUDO, "factor_range": [MIN_FACTOR, MAX_FACTOR]}}


def record_review(case_id: str, confirmed: bool):
    """Credit or debit every signal and rule that contributed to a reviewed case."""
    field = "confirmed" if confirmed else "overturned"
    db.write(f"""
        MATCH (c:Case {{id: $id}})
        UNWIND coalesce(c.signals, []) AS sig
        MERGE (s:SignalStat {{signal: sig}})
        ON CREATE SET s.confirmed = 0, s.overturned = 0
        SET s.{field} = s.{field} + 1""", {"id": case_id})
    db.write(f"""
        MATCH (c:Case {{id: $id}})-[:RAISED_FROM]->(al:Alert)
        MERGE (s:RuleStat {{rule: al.rule}})
        ON CREATE SET s.confirmed = 0, s.overturned = 0
        SET s.{field} = s.{field} + 1""", {"id": case_id})


def export_stats() -> dict:
    return {"signals": db.read("MATCH (s:SignalStat) RETURN s.signal AS signal, s.confirmed AS confirmed, s.overturned AS overturned"),
            "rules": db.read("MATCH (s:RuleStat) RETURN s.rule AS rule, s.confirmed AS confirmed, s.overturned AS overturned")}


def import_stats(stats: dict):
    """Carry learned tallies into a freshly loaded graph (used by the feedback evaluation)."""
    db.write("UNWIND $rows AS r MERGE (s:SignalStat {signal: r.signal}) SET s.confirmed = r.confirmed, s.overturned = r.overturned",
             {"rows": stats.get("signals", [])})
    db.write("UNWIND $rows AS r MERGE (s:RuleStat {rule: r.rule}) SET s.confirmed = r.confirmed, s.overturned = r.overturned",
             {"rows": stats.get("rules", [])})
