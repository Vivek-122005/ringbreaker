"""Deterministic investigation playbook over the same MCP tools the LLM uses.

Encodes the standard analyst procedure and a transparent, additive risk score. It runs when
no LLM key is configured, and it is the baseline in `python -m ringbreaker.evaluate`.
"""
from __future__ import annotations

from .agent import call_tool

HIGH_RISK_IP = ("vpn", "hosting")


async def run(session, tracker, alert_id: str):
    async def tool(name, **args):
        yield_ev = {"type": "tool", "name": name, "args": args}
        result = await call_tool(session, name, args)
        tracker.add(result)
        return yield_ev, result

    events = []

    async def t(name, **args):
        ev, result = await tool(name, **args)
        events.append(ev)
        events.append({"type": "result", "name": name, "data": result.get("data", result),
                       "evidence": result.get("evidence")})
        return result.get("data", result)

    def flush():
        out = list(events)
        events.clear()
        return out

    yield {"type": "step", "message": "Planner: investigation playbook (no LLM key configured). Tools discovered over MCP."}
    await t("get_policies")
    calib = await t("signal_calibration")
    weights = {x["signal"]: x for x in calib["signals"]}
    alert = await t("get_alert", alert_id=alert_id)
    if "error" in alert:
        for e in flush():
            yield e
        yield {"type": "error", "message": alert["error"]}
        return
    acc = alert["account"]
    prof = await t("account_profile", account_id=acc)
    prior = await t("prior_knowledge", entity_ids=[acc])
    shared = await t("shared_identifiers", account_id=acc)
    cluster = await t("identity_cluster", account_id=acc)
    out = await t("follow_money", account_id=acc, direction="out", max_hops=3, window_hours=72)
    inn = await t("follow_money", account_id=acc, direction="in", max_hops=2, window_hours=72)
    cyc = await t("find_cycles", account_id=acc, max_length=6)
    for e in flush():
        yield e

    score, evidence = 0.0, []
    def family(name: str) -> str:
        # households share a surname; companies ("Apex Traders Pvt Ltd") do not form households
        return name if name.endswith(("Ltd", "LLP", "Inc")) else name.split()[-1]

    surname = family(prof["owner_name"])

    def add(signal, claim, entities):
        """Add a signal at its current (analyst-calibrated) weight; show the calibration when it moved it."""
        nonlocal score
        w = weights.get(signal, {"weight": 0, "base_weight": 0, "reviews": 0})
        points = w["weight"]
        score += points
        ev = {"claim": claim, "entities": entities, "weight": round(points, 2), "signal": signal}
        if w["reviews"] and w["weight"] != w["base_weight"]:
            ev["calibration"] = f"weight {w['base_weight']} -> {w['weight']} after {w['reviews']} analyst reviews"
        evidence.append(ev)

    # --- account behaviour
    age = prof["account_age_days_at_last_activity"]
    pt = prof["pass_through_ratio"] or 0
    if pt >= 0.8 and prof["total_in"] >= 20_000:
        add("pass_through_new" if age < 120 else "pass_through_old", f"Pass-through account: received {prof['total_in']:,.0f} and sent {pt:.0%} onward"
                                        f"{'' if age >= 120 else f'; account only {age} days old'}", [acc])
    if prof["customer_since_days"] > 365 and age > 365:
        add("long_standing_customer", f"Long-standing customer ({prof['customer_since_days']} days)", [acc])
    if acc in prior.get("known_bad", []):
        add("prior_case", f"{acc} already linked to an earlier case or finding", [acc])
    elif acc in prior.get("analyst_cleared", []):
        add("analyst_cleared", f"An analyst overturned the earlier case on {acc} (cleared)", [acc])

    # --- identity pivot
    ring = {acc}
    for s in shared["shared"]:
        linked = [l["account"] for l in s["linked"]]
        unrelated = any(family(l["owner"]) != surname for l in s["linked"])
        risky_infra = any(k in (s.get("info") or "") for k in HIGH_RISK_IP) or "emulator" in (s.get("info") or "")
        if risky_infra and unrelated:
            add("risky_infra_shared", f"{s['kind']} {s['identifier']} ({s['info']}) shared with {s['linked_count']} unrelated customers",
                [acc, s["identifier"]] + linked[:6])
            ring.update(linked)
        elif s["kind"] == "Device" and unrelated and s["linked_count"] >= 2:
            add("device_shared_unrelated", f"Device {s['identifier']} ({s['info']}) used by {s['linked_count']} other unrelated customers",
                [acc, s["identifier"]] + linked[:6])
            ring.update(linked)
        elif s["kind"] in ("Phone", "Address") and unrelated and s["linked_count"] >= 1:
            add("identity_shared_unrelated", f"{s['kind']} {s['identifier']} shared by {s['linked_count']} customers with different surnames",
                [acc, s["identifier"]] + linked[:6])
            ring.update(linked)
        elif not unrelated and s["kind"] in ("IP", "Device", "Address"):
            add("household_only", f"{s['kind']} {s['identifier']} shared only within the {surname} household", [acc, s["identifier"]])
    if cluster.get("cluster_size", 0) >= 4 and cluster.get("distinct_owner_surnames", 0) >= 3 \
            and (cluster.get("opened_within_days") or 999) <= 30:
        add("identity_cluster", f"Identity cluster (WCC) of {cluster['cluster_size']} accounts with {cluster['distinct_owner_surnames']} "
                 f"different owners, all opened within {cluster['opened_within_days']} days",
            [a["account"] for a in cluster["accounts"]][:12])
        ring.update(a["account"] for a in cluster["accounts"])

    # --- money movement
    struct = sum(1 for p in out["paths"] for h in p["hops"][:1] if h["transfers"] >= 3 and h["amount"] / h["transfers"] < 10_000)
    if struct >= 2:
        add("structuring", f"Funds split into repeated sub-10,000 transfers to {struct} accounts (structuring)",
            [acc] + [p["path"][1] for p in out["paths"]][:8])
    flow_accounts = {a for p in out["paths"] for a in p["path"]}
    new_flow = ring & flow_accounts
    if len(new_flow) >= 3:
        add("fast_flow_linked", f"Money moved within hours through {len(new_flow) - 1} accounts that share identity elements",
            sorted(new_flow)[:10])
    if len(ring) > 1:
        ring.update(flow_accounts)
    senders = len({p["path"][0] for p in inn["paths"]})
    if senders >= 5 and age < 120:
        add("many_new_payers", f"{senders} different customers paid this new account shortly before it moved the money (possible scam victims)",
            [acc] + [p["path"][0] for p in inn["paths"]][:6])
    for c in cyc["cycles"]:
        if c["value_retained_pct"] >= 90:
            add("round_trip_loop", f"Round-trip loop through {len(set(c['accounts']))} accounts returned {c['value_retained_pct']}% "
                      f"of the value in {c['duration_days']} days", c["accounts"])
            ring.update(c["accounts"])
            break

    roles = None
    if len(ring) >= 3 and score >= 0.3:
        roles = await t("rank_roles", account_ids=sorted(ring))
        for e in flush():
            yield e
        hr = roles["money_to_high_risk_merchants"]
        if hr > 0:
            cash = [a["account"] for a in roles["accounts"] if a["high_risk_spend"] > 0]
            add("high_risk_cash_out", f"{hr:,} left the ring through crypto / gift-card / remittance merchants", cash)

    risk = round(max(0.0, min(1.0, score)), 2)
    if any("Round-trip" in e["claim"] for e in evidence if e["weight"] > 0):
        typology = "round_tripping"
    elif roles and any(a["role"] == "collector" for a in roles["accounts"]):
        typology = "app_scam_mule_network"
    elif any("Identity cluster" in e["claim"] or "Phone" in e["claim"] for e in evidence if e["weight"] > 0):
        typology = "synthetic_identity_bust_out"
    else:
        typology = "other" if risk >= 0.5 else "none"
    outcome = "ring_confirmed" if risk >= 0.8 else "suspicious_needs_review" if risk >= 0.5 else "false_positive"
    ring_accounts = sorted(ring) if outcome != "false_positive" else []

    # --- act (every action tool checks the bank's policies server-side; see policies.py)
    reasons = "; ".join(e["claim"] for e in evidence if e["weight"] > 0) or "no risk indicators beyond the rule hit"
    checks, case_id, freeze_id = [], None, None
    if outcome == "false_positive":
        benign = "; ".join(e["claim"] for e in evidence if e["weight"] < 0) or "no network risk found in the graph"
        res = await t("close_alert", alert_id=alert_id, disposition="false_positive", reason=f"{acc}: {benign}")
        checks.append({"tool": "close_alert", "status": res.get("status"), "policies": res.get("policies", [])})
    else:
        positive = [e for e in evidence if e["weight"] > 0]
        case = await t("open_case", alert_id=alert_id, account_ids=ring_accounts, typology=typology, risk=risk,
                       summary=reasons[:1500], signals=[e["signal"] for e in positive])
        case_id = cid = case["case"]
        ring_accounts = case.get("account_ids", ring_accounts)
        checks.append({"tool": "open_case", "status": "done", "policies": case.get("policies", [])})
        for e in positive[:5]:
            await t("record_finding", case_id=cid, entity_ids=e["entities"][:10], text=e["claim"])
        if roles:
            labelled = [a for a in roles["accounts"] if a["role"] in ("collector", "layering", "cash_out", "sink")]
            if labelled:
                await t("record_finding", case_id=cid, entity_ids=[a["account"] for a in labelled][:10],
                        family="network_structure",
                        text="Roles from PageRank and betweenness over the transfer graph: " +
                             ", ".join(f"{a['account']} {a['role'].replace('_', '-')}" for a in labelled[:8]))
        hold = [a["account"] for a in roles["accounts"] if a["role"] in ("collector", "layering", "cash_out", "sink")] \
            if roles else ring_accounts
        hold = [a for a in hold if a in ring_accounts] or ring_accounts
        fr = await t("freeze_accounts", case_id=cid, account_ids=hold, reason=f"{typology.replace('_', ' ')} (risk {risk})")
        freeze_id = fr.get("action_id")
        checks.append({"tool": "freeze_accounts", "status": fr.get("status"), "action_id": freeze_id,
                       "policies": fr.get("policies", [])})
        await t("close_alert", alert_id=alert_id, disposition="escalated_to_case", reason=f"Escalated to {cid}")
    for e in flush():
        yield e

    narrative = (f"Alert {alert_id} ({alert['rule']}) on {acc}. Risk {risk}: {outcome.replace('_', ' ')}"
                 + (f", typology {typology.replace('_', ' ')}, {len(ring_accounts)} accounts." if ring_accounts else ".")
                 + " Evidence: " + "; ".join(e["claim"] for e in evidence) + ".")
    verdict = {"outcome": outcome, "typology": typology, "risk": risk, "ring_accounts": ring_accounts,
               "evidence": evidence, "narrative": narrative, "roles": roles["accounts"] if roles else [],
               "case_id": case_id, "freeze_action_id": freeze_id, "policy_checks": checks}
    bad = tracker.check(verdict)
    verdict["unverified_ids_removed"] = bad
    yield {"type": "verdict", "verdict": verdict, "graph": tracker.edges}
