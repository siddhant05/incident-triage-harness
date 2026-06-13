"""FastAPI app. Webhook endpoint + run inspection + replay."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .agents.heuristic_agent import HeuristicAgent
from .persistence import Store
from .pipeline import Pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
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
            "GET /health": "Health check",
        },
        "docs": "See HARNESS.md in the repo for architecture.",
    }


@app.get("/runs")
def list_runs(limit: int = 20) -> dict[str, Any]:
    return {"runs": _store.list_runs(limit=limit)}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    data = _store.get_run(run_id)
    if not data.get("run"):
        raise HTTPException(404, "run not found")
    return data


@app.post("/webhook/sentry")
async def webhook_sentry(request: Request) -> dict[str, Any]:
    payload = await request.json()
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
