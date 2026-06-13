# Incident Triage Harness

Governed AI agent for first-pass Sentry/Datadog alert triage. See [HARNESS.md](HARNESS.md) for architecture.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Optional: real git analysis via GitHub REST API (no local clone needed)
export GITHUB_REPO=psf/requests
# Optional: raise REST rate limit from 60/hr to 5000/hr
export GITHUB_TOKEN=ghp_...

# Back-compat: if GITHUB_REPO is unset, a local clone path still works
# git clone --depth 50 https://github.com/psf/requests ./demo-repo
# export DEMO_GIT_REPO="$PWD/demo-repo"

# CLI replay (no API key needed; heuristic agent)
python -m demo.replay demo/sample_payload.json

# With Claude agent
export ANTHROPIC_API_KEY=sk-...
python -m demo.replay demo/sample_payload.json --agent claude

# Run the webhook service
uvicorn harness.main:app --reload --port 8000

# Trigger via webhook
curl -X POST 'http://localhost:8000/webhook/sentry?agent=heuristic' \
  -H 'content-type: application/json' \
  -d @demo/sample_payload.json
```

## Tests

```bash
pip install pytest
pytest -q tests/
```

## Evals

End-to-end behavioral evals live in `evals/`. Each case is a JSON fixture in
`evals/cases/` with a Sentry payload plus an `expected` block describing the
RunResult fields that must hold (status, routed team, min confidence,
final action, alarms, suspect-file substrings).

```bash
# Run all cases against the deterministic heuristic agent (no API key needed)
python -m evals.run_eval --agent heuristic

# Run a single case
python -m evals.run_eval --agent heuristic --case users_api_null_pointer

# Point at a custom fixtures directory
python -m evals.run_eval --cases-dir evals/cases

# LLM-backed agents (may surface flaky cases; needs API key)
export ANTHROPIC_API_KEY=sk-...
python -m evals.run_eval --agent claude

export GEMINI_API_KEY=...
python -m evals.run_eval --agent gemini
```

Exit code is `0` if every case passes, `1` otherwise. Each run uses a fresh
SQLite store (so dedup never short-circuits) and passes `git_repo=""` to the
Pipeline so evals do not depend on a network or local clone.

The heuristic agent is the deterministic baseline and is the one wired into
`tests/test_evals.py`. LLM-backed agents (`gemini`, `claude`) may produce
flaky results — treat their pass rate as a signal rather than a gate.

## Live Sentry Integration

To receive real Sentry webhooks (HMAC-verified) at the deployed harness, follow
[SENTRY_SETUP.md](SENTRY_SETUP.md). Set `SENTRY_CLIENT_SECRET` on Render to enable
signature checks; unset it for the demo/dev curl flow.

## Demo flow

1. Start service: `uvicorn harness.main:app --port 8000`
2. POST `demo/sample_payload.json` → see pipeline produce Slack message + Jira stub
3. POST `demo/payload_malformed.json` → see guardrail reject + alarm
4. Swap agent: POST same payload with `?agent=claude` → identical harness path, different `Proposal`
5. List runs: `GET /runs`, inspect a run: `GET /runs/<id>`
