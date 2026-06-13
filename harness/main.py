"""FastAPI app. Webhook endpoint + run inspection + replay."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .agents.heuristic_agent import HeuristicAgent
from .observability import configure_logging, metrics
from .persistence import Store
from .pipeline import Pipeline


# Approximate per-million-token USD pricing. Treat tokens_used as 50/50 input/output.
COST_PER_MTOKEN_USD = {
    "gemini": {"input": 0.30, "output": 2.50},   # gemini-2.5-flash approx
    "claude": {"input": 1.00, "output": 5.00},   # claude-haiku-4-5 approx
    "heuristic": {"input": 0.0, "output": 0.0},
}


def _estimate_cost_usd(agent_name: str, tokens: int) -> float:
    rates = COST_PER_MTOKEN_USD.get((agent_name or "").lower(), {"input": 0.0, "output": 0.0})
    half = tokens / 2.0
    return (half * rates["input"] + half * rates["output"]) / 1_000_000.0


def _read_daily_budget() -> int:
    """Read budget.daily_token_cap from guardrails.yaml without hard YAML dep."""
    path = Path(__file__).parent / "guardrails.yaml"
    try:
        import yaml  # type: ignore
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        cap = (data.get("budget") or {}).get("daily_token_cap")
        if cap is not None:
            return int(cap)
    except Exception:
        # Fallback: tiny manual parse for the one key we need.
        try:
            in_budget = False
            with open(path) as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if not line.startswith(" ") and line.strip().endswith(":"):
                        in_budget = line.strip() == "budget:"
                        continue
                    if in_budget and "daily_token_cap" in line:
                        return int(line.split(":", 1)[1].strip())
        except Exception:
            pass
    return 0


def compute_cost(store: Store) -> dict[str, Any]:
    """Aggregate token spend + cost from runs+stages. Used by /cost and tests."""
    rows: list[sqlite3.Row]
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT r.run_id, r.agent_name, r.started_at, s.output_json
            FROM runs r LEFT JOIN stages s
              ON s.run_id = r.run_id AND s.stage_name = 'agent'
            """
        ).fetchall()

    # Collapse duplicate (run_id) when multiple agent stages exist: take max tokens per run.
    per_run: dict[str, dict[str, Any]] = {}
    for r in rows:
        run_id = r["run_id"]
        agent_name = (r["agent_name"] or "").lower() or "heuristic"
        started_at = r["started_at"] or 0.0
        tokens = 0
        if r["output_json"]:
            try:
                out = json.loads(r["output_json"])
                tokens = int(out.get("tokens_used", 0) or 0)
            except Exception:
                tokens = 0
        prev = per_run.get(run_id)
        if prev is None or tokens > prev["tokens"]:
            per_run[run_id] = {"agent": agent_name, "started_at": started_at, "tokens": tokens}

    total_tokens = 0
    total_runs = len(per_run)
    runs_with_llm = 0
    by_agent_tokens: dict[str, int] = defaultdict(int)
    by_agent_runs: dict[str, int] = defaultdict(int)
    by_day_tokens: dict[str, int] = defaultdict(int)
    by_day_runs: dict[str, int] = defaultdict(int)

    today_utc = datetime.now(timezone.utc).date()
    today_tokens = 0

    for info in per_run.values():
        agent = info["agent"]
        tokens = info["tokens"]
        total_tokens += tokens
        by_agent_tokens[agent] += tokens
        by_agent_runs[agent] += 1
        if agent != "heuristic":
            runs_with_llm += 1
        if info["started_at"]:
            d = datetime.fromtimestamp(info["started_at"], tz=timezone.utc).date()
            day_str = d.isoformat()
            by_day_tokens[day_str] += tokens
            by_day_runs[day_str] += 1
            if d == today_utc:
                today_tokens += tokens

    by_agent: dict[str, dict[str, Any]] = {}
    for agent, runs in by_agent_runs.items():
        toks = by_agent_tokens[agent]
        by_agent[agent] = {
            "tokens": toks,
            "runs": runs,
            "avg_tokens_per_run": (toks / runs) if runs else 0.0,
            "est_cost_usd": round(_estimate_cost_usd(agent, toks), 6),
        }

    # last 7 days, oldest -> newest, including zero days
    by_day: list[dict[str, Any]] = []
    for i in range(6, -1, -1):
        d = today_utc - timedelta(days=i)
        key = d.isoformat()
        by_day.append({
            "date": key,
            "tokens": by_day_tokens.get(key, 0),
            "runs": by_day_runs.get(key, 0),
        })

    daily_budget = _read_daily_budget()
    daily_pct = (today_tokens / daily_budget) if daily_budget else 0.0

    return {
        "total_tokens": total_tokens,
        "total_runs": total_runs,
        "runs_with_llm": runs_with_llm,
        "by_agent": by_agent,
        "by_day": by_day,
        "daily_budget": daily_budget,
        "daily_budget_pct_used": round(daily_pct, 4),
    }

configure_logging(
    json=os.environ.get("LOG_JSON", "1") != "0",
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
log = logging.getLogger("harness.main")


def _make_agent(name: str):
    name = (name or os.environ.get("HARNESS_AGENT", "heuristic")).lower()
    if name == "heuristic":
        return HeuristicAgent()
    if name == "claude":
        from .agents.claude_agent import ClaudeAgent
        return ClaudeAgent()
    if name == "gemini":
        from .agents.gemini_agent import GeminiAgent
        return GeminiAgent()
    raise ValueError(f"unknown agent: {name}")


app = FastAPI(title="Incident Triage Harness")
_store = Store()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "incident-triage-harness",
        "endpoints": {
            "POST /webhook/sentry?agent=<heuristic|gemini|claude>": "Process a Sentry webhook payload",
            "POST /replay/{run_id}?agent=<...>": "Re-run a persisted incident",
            "GET /runs": "List recent runs",
            "GET /runs/{run_id}": "Inspect a run (stages + alarms)",
            "GET /cost": "Token spend + estimated USD cost dashboard",
            "GET /health": "Health check",
        },
        "docs": "See HARNESS.md in the repo for architecture.",
    }


@app.get("/runs")
def list_runs(limit: int = 20) -> dict[str, Any]:
    return {"runs": _store.list_runs(limit=limit)}


@app.get("/cost")
def cost_dashboard() -> dict[str, Any]:
    return compute_cost(_store)


@app.get("/metrics")
def metrics_endpoint() -> dict[str, Any]:
    return metrics(_store)


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    data = _store.get_run(run_id)
    if not data.get("run"):
        raise HTTPException(404, "run not found")
    return data


_SENTRY_DEV_WARNED = False
# Sentry resource types that should run the triage pipeline. Others are ack'd 200 + skipped.
_SENTRY_HANDLED_RESOURCES = {"event_alert", "issue", "error"}


def _verify_sentry_signature(secret: str, body: bytes, signature: str) -> bool:
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


@app.post("/webhook/sentry")
async def webhook_sentry(request: Request) -> dict[str, Any]:
    global _SENTRY_DEV_WARNED
    raw_body = await request.body()

    secret = os.environ.get("SENTRY_CLIENT_SECRET")
    if secret:
        sig = request.headers.get("Sentry-Hook-Signature", "")
        if not _verify_sentry_signature(secret, raw_body, sig):
            raise HTTPException(401, "invalid signature")
    else:
        if not _SENTRY_DEV_WARNED:
            log.warning(
                "SENTRY_CLIENT_SECRET not set; webhook signature verification disabled (dev mode)."
            )
            _SENTRY_DEV_WARNED = True

    # Resource filter: only handle issue/event alerts. Ack others with 200 so Sentry doesn't retry.
    resource = request.headers.get("Sentry-Hook-Resource", "")
    if resource and resource not in _SENTRY_HANDLED_RESOURCES:
        return {"ignored": True, "resource": resource}

    try:
        payload = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "invalid json")

    agent_name = request.query_params.get("agent", os.environ.get("HARNESS_AGENT", "heuristic"))
    try:
        agent = _make_agent(agent_name)
    except Exception as e:
        raise HTTPException(400, str(e))
    pipeline = Pipeline(agent=agent, store=_store, git_repo=os.environ.get("GITHUB_REPO") or os.environ.get("DEMO_GIT_REPO", ""))
    result = pipeline.run(payload)
    return result.to_dict()


@app.post("/replay/{run_id}")
def replay(run_id: str, agent: str | None = None) -> dict[str, Any]:
    """Replay a run from its persisted material. Same payload, fresh pipeline."""
    data = _store.get_run(run_id)
    if not data.get("run"):
        raise HTTPException(404, "run not found")
    payload = None
    for s in data["stages"]:
        if s["stage_name"] == "material":
            import json
            payload = json.loads(s["input_json"])
            break
    if payload is None:
        raise HTTPException(400, "no material stage found")
    new_agent = _make_agent(agent or data["run"].get("agent_name") or "heuristic")
    pipeline = Pipeline(agent=new_agent, store=_store, git_repo=os.environ.get("GITHUB_REPO") or os.environ.get("DEMO_GIT_REPO", ""))
    result = pipeline.run(payload)
    return result.to_dict()
