"""The RingBreaker investigator.

The agent connects to the RingBreaker MCP server, discovers the tool catalogue and works an
alert the way a financial-crime analyst does: profile the account, pivot on shared identity
elements, follow the money, find the ring, decide, act, and write down why.

Two planners drive the same MCP tools:
  * llm       an LLM picks tools through function calling (Gemini / Groq / OpenRouter, any
              OpenAI-compatible endpoint)
  * playbook  a deterministic investigation playbook, used when no LLM key is configured and
              as the baseline the LLM planner is measured against

Either way the final verdict is validated: it may only cite accounts that appeared in tool
results, so every claim is backed by a path in the graph.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-flash-lite-latest"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free"),
}
OUTCOMES = ["ring_confirmed", "suspicious_needs_review", "false_positive"]
TYPOLOGIES = ["app_scam_mule_network", "round_tripping", "synthetic_identity_bust_out", "other", "none"]
MAX_STEPS = 18

SYSTEM = """You are RingBreaker, a financial-crime investigator at a retail bank. A transaction-monitoring
alert has fired. Most alerts are false positives (families sharing a home network, payroll, landlords);
some are one visible edge of a money-mule network, a round-tripping loop or a synthetic-identity ring.

Work the alert like a senior analyst, using only the graph tools:
1. get_alert, then account_profile and prior_knowledge for the flagged account.
2. Pivot: shared_identifiers and identity_cluster (who else controls this account's devices, IPs, phones, addresses?).
3. Follow the money: follow_money out and in; find_cycles for loops.
4. If a ring emerges, rank_roles over the candidate accounts to label collector / layering / cash-out.
5. Use explain_connection when you need to justify why two accounts belong together.
6. Decide. If it is a ring (risk >= 0.5): open_case with every ring account, record_finding for the key evidence,
   freeze_accounts for the accounts still holding or moving funds (the tool enforces bank policy), then
   close_alert as escalated_to_case. If it is benign: close_alert as false_positive with the reason.
7. Finish by calling submit_verdict.

Rules:
- Never invent account ids. Use only ids returned by tools.
- An empty result means the graph has no such link, not that the account is innocent.
- Benign signals: long customer tenure, shared identifiers between people with the same surname at one address,
  regular salary-sized or rent-sized payments. Bad signals: new accounts, pass-through ratio near 1, many unrelated
  people on one device or VPN/hosting IP, structuring just under 10,000, funds ending at crypto/gift-card merchants,
  loops that return ~95%%+ of value.
- Be efficient: you have at most %d tool calls. Keep thinking short.""" % MAX_STEPS

SUBMIT_VERDICT = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "Submit the final investigation verdict. Every account id must have appeared in a tool result.",
        "parameters": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": OUTCOMES},
                "typology": {"type": "string", "enum": TYPOLOGIES},
                "risk": {"type": "number", "description": "0..1"},
                "ring_accounts": {"type": "array", "items": {"type": "string"}},
                "evidence": {"type": "array", "description": "Each claim with the entity ids that prove it",
                             "items": {"type": "object", "properties": {
                                 "claim": {"type": "string"},
                                 "entities": {"type": "array", "items": {"type": "string"}}},
                                 "required": ["claim", "entities"]}},
                "narrative": {"type": "string", "description": "Plain-English case summary for the analyst / SAR draft"},
            },
            "required": ["outcome", "typology", "risk", "ring_accounts", "evidence", "narrative"],
        },
    },
}


@asynccontextmanager
async def toolbox():
    """Connect to the RingBreaker MCP server over stdio and yield (session, tool specs)."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "ringbreaker.mcp_server"],
                                   cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   env={**os.environ})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            listed = await s.list_tools()
            specs = [{"type": "function", "function": {"name": t.name, "description": t.description or "",
                                                         "parameters": _clean_schema(t.inputSchema)}}
                     for t in listed.tools if t.name != "list_open_alerts"]
            yield s, specs


def _clean_schema(schema: dict) -> dict:
    """Strip JSON-schema keys some OpenAI-compatible providers reject."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items() if k not in ("title", "additionalProperties", "$schema")}
    if isinstance(schema, list):
        return [_clean_schema(x) for x in schema]
    return schema


async def call_tool(session: ClientSession, name: str, args: dict) -> dict:
    res = await session.call_tool(name, args)
    if res.structuredContent is not None:
        out = res.structuredContent
        return out.get("result", out) if isinstance(out, dict) and set(out) == {"result"} else out
    text = "".join(getattr(c, "text", "") for c in res.content or [])
    try:
        out = json.loads(text) if text else {}
    except json.JSONDecodeError:
        out = {"error": text[:1000]}
    if res.isError and not (isinstance(out, dict) and "error" in out):
        out = {"error": text[:1000]}
    return out


class Tracker:
    """Collects every entity id the tools have surfaced; the verdict may only cite these."""

    def __init__(self):
        self.seen: set[str] = set()
        self.edges: list = []

    def add(self, result: dict):
        ev = result.get("evidence") or {}
        self.seen.update(ev.get("nodes", []))
        self.edges += ev.get("edges", [])

    def check(self, verdict: dict) -> list[str]:
        cited = set(verdict.get("ring_accounts", []))
        for e in verdict.get("evidence", []):
            cited.update(e.get("entities", []))
        return sorted(c for c in cited if c not in self.seen)


def provider() -> tuple[str, str, str, str] | None:
    name = os.getenv("RB_LLM_PROVIDER", "").lower()
    order = [name] if name else list(PROVIDERS)
    for n in order:
        if n in PROVIDERS and os.getenv(PROVIDERS[n][1]):
            base, env, model = PROVIDERS[n]
            return n, base, os.environ[env], os.getenv("RB_MODEL", model)
    return None


def _brief(result: dict, limit: int = 5000) -> str:
    s = json.dumps(result.get("data", result), default=str)
    return s if len(s) <= limit else s[:limit] + '..."(truncated)"'


async def investigate(alert_id: str, planner: str = "auto") -> AsyncIterator[dict]:
    """Investigate one alert, yielding events: step, tool, result, verdict, error."""
    llm = provider() if planner in ("auto", "llm") else None
    if planner == "llm" and not llm:
        yield {"type": "error", "message": "No LLM key configured (GEMINI_API_KEY / GROQ_API_KEY / OPENROUTER_API_KEY)."}
        return
    async with toolbox() as (session, specs):
        tracker = Tracker()
        if llm:
            async for ev in _llm_loop(session, specs, tracker, alert_id, llm):
                yield ev
        else:
            from .playbook import run as playbook
            async for ev in playbook(session, tracker, alert_id):
                yield ev


async def _chat(http: httpx.AsyncClient, base: str, key: str, body: dict, retries: int = 6) -> httpx.Response:
    """POST a chat completion, waiting out free-tier rate limits (HTTP 429 / 503) before giving up."""
    for attempt in range(retries + 1):
        resp = await http.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json=body)
        if resp.status_code not in (429, 503) or attempt == retries:
            resp.raise_for_status()
            return resp
        hint = resp.headers.get("retry-after") or (re.search(r"retry in ([\d.]+)s", resp.text) or [None, None])[1]
        await asyncio.sleep(min(60.0, float(hint) + 1 if hint else 5.0 * 2 ** attempt))
    return resp


async def _llm_loop(session, specs, tracker, alert_id, llm):
    name, base, key, model = llm
    yield {"type": "step", "message": f"Planner: {name} / {model}. Tools discovered over MCP: {len(specs)}"}
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Investigate alert {alert_id}."}]
    tools = specs + [SUBMIT_VERDICT]
    async with httpx.AsyncClient(timeout=90) as http:
        for step in range(MAX_STEPS + 3):
            try:
                resp = await _chat(http, base, key, {"model": model, "messages": messages, "tools": tools,
                                                     "tool_choice": "auto", "temperature": 0.1})
            except httpx.HTTPError as e:
                body = getattr(getattr(e, "response", None), "text", "")[:300]
                yield {"type": "error", "message": f"LLM call failed: {e} {body}"}
                return
            msg = resp.json()["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if msg.get("content"):
                yield {"type": "thought", "message": msg["content"]}
            messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                messages.append({"role": "user", "content": "Continue the investigation, then call submit_verdict."})
                continue
            for c in calls:
                fn = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if fn == "submit_verdict":
                    bad = tracker.check(args)
                    if bad and step < MAX_STEPS + 2:
                        note = f"Rejected: these ids never appeared in tool results: {bad}. Cite only graph evidence."
                        yield {"type": "validation", "message": note}
                        messages.append({"role": "tool", "tool_call_id": c["id"], "content": note})
                        continue
                    args["unverified_ids_removed"] = bad
                    args["ring_accounts"] = [a for a in args.get("ring_accounts", []) if a not in bad]
                    yield {"type": "verdict", "verdict": args, "graph": tracker.edges}
                    return
                yield {"type": "tool", "name": fn, "args": args}
                result = await call_tool(session, fn, args)
                tracker.add(result)
                yield {"type": "result", "name": fn, "data": result.get("data", result),
                       "evidence": result.get("evidence")}
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": _brief(result)})
            if step == MAX_STEPS - 1:
                messages.append({"role": "user", "content": "Tool budget reached. Take actions if needed and call submit_verdict now."})
    yield {"type": "error", "message": "Investigation did not converge."}
