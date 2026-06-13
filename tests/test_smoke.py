"""Smoke tests. Run: python -m pytest -q tests/"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

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


def test_collect_git_context_github_rest_shape():
    """`owner/repo` spec uses REST and returns the canonical schema."""
    list_body = [
        {"sha": "abc123", "commit": {"author": {"name": "Alice"}, "message": "fix: bug\n\nbody"}},
        {"sha": "def456", "commit": {"author": {"name": "Bob"}, "message": "feat: thing"}},
    ]
    detail_body_1 = {"files": [{"filename": "a.py"}, {"filename": "b.py"}]}
    detail_body_2 = {"files": [{"filename": "c.py"}]}

    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/commits?per_page=5"):
            return _mock_response(200, list_body)
        if url.endswith("/commits/abc123"):
            return _mock_response(200, detail_body_1)
        if url.endswith("/commits/def456"):
            return _mock_response(200, detail_body_2)
        return _mock_response(404, {})

    with patch("harness.tools.requests.get", side_effect=fake_get):
        ctx = tools.collect_git_context("owner/repo", "svc", ["x.py"])

    assert ctx["available"] is True
    assert ctx["repo"] == "owner/repo"
    assert ctx["blame"] == []
    assert len(ctx["recent_commits"]) == 2
    first = ctx["recent_commits"][0]
    assert set(first.keys()) == {"sha", "author", "msg", "files_changed"}
    assert first["sha"] == "abc123"
    assert first["author"] == "Alice"
    assert first["msg"] == "fix: bug"
    assert first["files_changed"] == ["a.py", "b.py"]


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
