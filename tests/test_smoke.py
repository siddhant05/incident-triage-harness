"""Smoke tests. Run: python -m pytest -q tests/"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from harness.agents.heuristic_agent import HeuristicAgent
from harness.material import IncidentMaterial, parse_sentry_payload
from harness.persistence import Store
from harness.pipeline import Pipeline
from harness.routing import TeamRouter
from harness import tools


DEMO = Path(__file__).resolve().parents[1] / "demo"


def _payload(name: str):
    return json.loads((DEMO / name).read_text())


def test_material_parses_tags_and_frames():
    m = parse_sentry_payload(_payload("sample_payload.json"))
    assert m.event_id == "evt-001-demo"
    assert m.service == "users-api"
    assert m.severity == "P2"
    assert any("sessions.py" in f for f in m.stack_files)


def test_pipeline_runs_with_heuristic(tmp_path):
    store = Store(tmp_path / "h.db")
    p = Pipeline(agent=HeuristicAgent(), store=store, git_repo="")
    r = p.run(_payload("sample_payload.json"))
    assert r.status in {"ok", "escalated"}
    assert r.proposal is not None
    assert any(c["name"] == "signal_sufficiency" for c in r.checkpoints)


def test_pipeline_rejects_malformed(tmp_path):
    store = Store(tmp_path / "h.db")
    p = Pipeline(agent=HeuristicAgent(), store=store, git_repo="")
    r = p.run(_payload("payload_malformed.json"))
    assert r.status == "rejected"
    assert any(a["name"] == "INPUT_INVALID" for a in r.alarms)


def test_dedup_returns_cached(tmp_path):
    store = Store(tmp_path / "h.db")
    p = Pipeline(agent=HeuristicAgent(), store=store, git_repo="")
    r1 = p.run(_payload("sample_payload.json"))
    r2 = p.run(_payload("sample_payload.json"))
    if r1.status in {"ok", "escalated"}:
        assert r2.status == "cached"


def test_swap_agent_same_pipeline_path(tmp_path):
    """Swap test: heuristic agent runs same pipeline shape."""
    store1 = Store(tmp_path / "a.db")
    store2 = Store(tmp_path / "b.db")
    p1 = Pipeline(agent=HeuristicAgent(), store=store1, git_repo="")
    p2 = Pipeline(agent=HeuristicAgent(), store=store2, git_repo="")
    r1 = p1.run(_payload("sample_payload.json"))
    r2 = p2.run(_payload("sample_payload.json"))
    assert {c["name"] for c in r1.checkpoints} == {c["name"] for c in r2.checkpoints}


def _mock_response(status: int, json_body, headers=None):
    m = MagicMock()
    m.status_code = status
    m.headers = headers or {}
    m.json.return_value = json_body
    return m


def test_collect_git_context_github_path_scoped():
    """Path-scoped: for each stack file, list commits touching it; dedup by SHA."""
    list_for_a = [
        {"sha": "abc123", "commit": {"author": {"name": "Alice"}, "message": "fix: bug\n\nbody"}},
    ]
    list_for_b = [
        {"sha": "abc123", "commit": {"author": {"name": "Alice"}, "message": "fix: bug"}},
        {"sha": "def456", "commit": {"author": {"name": "Bob"}, "message": "feat: thing"}},
    ]
    detail_abc = {"files": [{"filename": "a.py"}, {"filename": "b.py"}]}
    detail_def = {"files": [{"filename": "b.py"}]}

    def fake_get(url, headers=None, timeout=None):
        if "commits?per_page=3&path=a.py" in url:
            return _mock_response(200, list_for_a)
        if "commits?per_page=3&path=b.py" in url:
            return _mock_response(200, list_for_b)
        if url.endswith("/commits/abc123"):
            return _mock_response(200, detail_abc)
        if url.endswith("/commits/def456"):
            return _mock_response(200, detail_def)
        return _mock_response(404, {})

    with patch("harness.tools.requests.get", side_effect=fake_get):
        ctx = tools.collect_git_context("owner/repo", "svc", ["a.py", "b.py"])

    assert ctx["available"] is True
    assert ctx["repo"] == "owner/repo"
    assert ctx["scope"] == "path-scoped"
    assert ctx["queried_paths"] == ["a.py", "b.py"]
    shas = {c["sha"] for c in ctx["recent_commits"]}
    assert shas == {"abc123", "def456"}  # deduped


def test_collect_git_context_github_fallback_when_path_empty():
    """If path queries return no commits, fall back to recent commits."""
    list_for_path = []
    list_recent = [
        {"sha": "ghi789", "commit": {"author": {"name": "Carol"}, "message": "recent change"}},
    ]
    detail_ghi = {"files": [{"filename": "z.py"}]}

    def fake_get(url, headers=None, timeout=None):
        if "&path=" in url:
            return _mock_response(200, list_for_path)
        if "commits?per_page=5" in url:
            return _mock_response(200, list_recent)
        if url.endswith("/commits/ghi789"):
            return _mock_response(200, detail_ghi)
        return _mock_response(404, {})

    with patch("harness.tools.requests.get", side_effect=fake_get):
        ctx = tools.collect_git_context("owner/repo", None, ["a.py"])

    assert ctx["available"] is True
    assert ctx["scope"] == "recent"
    assert {c["sha"] for c in ctx["recent_commits"]} == {"ghi789"}


def test_collect_git_context_github_404():
    def fake_get(url, headers=None, timeout=None):
        return _mock_response(404, {"message": "Not Found"})

    with patch("harness.tools.requests.get", side_effect=fake_get):
        ctx = tools.collect_git_context("owner/missing", None, [])
    assert ctx["available"] is False
    assert "404" in ctx["reason"]


def test_collect_git_context_rate_limited():
    def fake_get(url, headers=None, timeout=None):
        return _mock_response(403, {}, headers={"X-RateLimit-Remaining": "0"})

    with patch("harness.tools.requests.get", side_effect=fake_get):
        ctx = tools.collect_git_context("owner/repo", None, [])
    assert ctx["available"] is False
    assert "rate limit" in ctx["reason"].lower()


def test_collect_git_context_local_path_fallback(tmp_path):
    """Non-`owner/repo` spec without .git returns the local-path error."""
    ctx = tools.collect_git_context(str(tmp_path), None, [])
    assert ctx["available"] is False
    assert "no git repo" in ctx["reason"]


# ----- team routing -----

def _mat(service="users-api", tags=None, stack_files=None, severity="P2"):
    frames = []
    if stack_files:
        from harness.material import StackFrame
        frames = [StackFrame(filename=f, function="x", lineno=1) for f in stack_files]
    return IncidentMaterial(
        event_id="x", service=service, severity=severity,
        title="t", message="m", tags=tags or [], stack_frames=frames,
    )


def test_routing_by_service_prefix_and_tag():
    r = TeamRouter()
    route = r.route(_mat(service="ads-tracker", tags=["ads:true", "campaign:cpc"]), None)
    assert route.team_id == "ads-platform"
    assert any("service_prefix:ads-" in m for m in route.matched_on)


def test_routing_by_runbook_id():
    r = TeamRouter()
    route = r.route(_mat(service="random-svc"), {"id": "null_pointer_python"})
    assert route.team_id == "identity"
    assert any("runbook:" in m for m in route.matched_on)


def test_routing_unrouted_fallback():
    r = TeamRouter()
    route = r.route(_mat(service="totally-unknown"), None)
    assert route.unrouted is True
    assert route.team_id == "unrouted"


def test_routing_by_path_prefix():
    r = TeamRouter()
    route = r.route(_mat(service="x", stack_files=["src/ads/tracker.py"]), None)
    assert route.team_id == "ads-platform"


# ----- cost dashboard -----

def test_cost_dashboard_aggregates_tokens(tmp_path):
    from harness.main import compute_cost, _estimate_cost_usd

    store = Store(tmp_path / "cost.db")
    p = Pipeline(agent=HeuristicAgent(), store=store, git_repo="")

    # Use 3 distinct payloads so dedup doesn't collapse them.
    base = _payload("sample_payload.json")
    run_ids: list[str] = []
    for i in range(3):
        payload = json.loads(json.dumps(base))
        # mutate event_id to bypass dedup
        if "event" in payload and isinstance(payload["event"], dict):
            payload["event"]["event_id"] = f"evt-cost-{i}"
        else:
            payload["event_id"] = f"evt-cost-{i}"
        r = p.run(payload)
        run_ids.append(r.run_id)
        # Simulate LLM spend on top of the heuristic run.
        store.record_stage(r.run_id, "agent", {}, {"tokens_used": 1000}, passed=True)

    # Force one run's agent_name to look like a gemini run for cost math.
    import sqlite3
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE runs SET agent_name = 'gemini' WHERE run_id = ?", (run_ids[0],))
        c.commit()

    cost = compute_cost(store)

    # 3 runs, each contributing 1000 tokens via the manually recorded agent stage.
    assert cost["total_runs"] == 3
    assert cost["total_tokens"] == 3000
    # One run is gemini -> counts as llm.
    assert cost["runs_with_llm"] == 1
    assert "heuristic" in cost["by_agent"]
    assert cost["by_agent"]["heuristic"]["tokens"] == 2000
    assert cost["by_agent"]["heuristic"]["runs"] == 2
    assert cost["by_agent"]["heuristic"]["est_cost_usd"] == 0.0
    assert cost["by_agent"]["gemini"]["tokens"] == 1000
    expected_gemini_cost = round(_estimate_cost_usd("gemini", 1000), 6)
    assert cost["by_agent"]["gemini"]["est_cost_usd"] == expected_gemini_cost
    # by_day has exactly 7 entries.
    assert len(cost["by_day"]) == 7
    # daily budget loaded from guardrails.yaml (500000).
    assert cost["daily_budget"] == 500000


# ----- observability -----

def test_observability_run_id_in_json_log():
    import logging as _logging
    from harness.observability import JsonFormatter, RunIdFilter, set_run_id

    records: list[_logging.LogRecord] = []

    class _Capture(_logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = _logging.getLogger("harness.test.obs")
    logger.setLevel(_logging.INFO)
    handler = _Capture()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunIdFilter())
    logger.addHandler(handler)
    logger.propagate = False

    try:
        set_run_id("rid-xyz")
        logger.info("hello", extra={"foo": "bar"})
        assert records, "no log captured"
        line = handler.format(records[-1])
        payload = json.loads(line)
        assert payload["run_id"] == "rid-xyz"
        assert payload["msg"] == "hello"
        assert payload["foo"] == "bar"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "harness.test.obs"
    finally:
        set_run_id(None)
        logger.removeHandler(handler)


def test_metrics_shape_after_rejected(tmp_path):
    from harness.observability import metrics
    store = Store(tmp_path / "m.db")
    p = Pipeline(agent=HeuristicAgent(), store=store, git_repo="")
    p.run(_payload("payload_malformed.json"))
    m = metrics(store)
    assert m["runs_total"] == 1
    assert m["runs_by_status"]["rejected"] == 1
    assert m["runs_by_agent"].get("heuristic") == 1
    assert m["alarms_by_name"].get("INPUT_INVALID", 0) >= 1
    assert isinstance(m["alarms_by_severity"], dict)
    assert {"LOW", "MEDIUM", "HIGH", "CRITICAL"}.issubset(m["alarms_by_severity"].keys())
    assert m["last_run_ts"] > 0


# ----- sentry webhook signature verification -----

def _sentry_post(client, body_bytes, headers):
    return client.post("/webhook/sentry", content=body_bytes, headers=headers)


def test_webhook_rejects_bad_signature():
    from harness.main import app
    os.environ["SENTRY_CLIENT_SECRET"] = "secret"
    try:
        client = TestClient(app)
        body = json.dumps(_payload("sample_payload.json")).encode()
        resp = _sentry_post(client, body, {
            "content-type": "application/json",
            "Sentry-Hook-Signature": "deadbeef",
            "Sentry-Hook-Resource": "event_alert",
        })
        assert resp.status_code == 401
    finally:
        os.environ.pop("SENTRY_CLIENT_SECRET", None)


def test_webhook_accepts_valid_signature():
    from harness.main import app
    secret = "secret"
    os.environ["SENTRY_CLIENT_SECRET"] = secret
    try:
        client = TestClient(app)
        body = json.dumps(_payload("sample_payload.json")).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        resp = _sentry_post(client, body, {
            "content-type": "application/json",
            "Sentry-Hook-Signature": sig,
            "Sentry-Hook-Resource": "event_alert",
        })
        assert resp.status_code == 200, resp.text
    finally:
        os.environ.pop("SENTRY_CLIENT_SECRET", None)


def test_dashboard_serves_html():
    from harness.main import app
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "Incident Triage Harness" in resp.text


def test_webhook_ignores_other_resources():
    from harness.main import app
    secret = "secret"
    os.environ["SENTRY_CLIENT_SECRET"] = secret
    try:
        client = TestClient(app)
        body = json.dumps({"action": "created"}).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        resp = _sentry_post(client, body, {
            "content-type": "application/json",
            "Sentry-Hook-Signature": sig,
            "Sentry-Hook-Resource": "comment",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("ignored") is True
        assert data.get("resource") == "comment"
    finally:
        os.environ.pop("SENTRY_CLIENT_SECRET", None)
