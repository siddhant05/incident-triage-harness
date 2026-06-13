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

## Demo flow

1. Start service: `uvicorn harness.main:app --port 8000`
2. POST `demo/sample_payload.json` → see pipeline produce Slack message + Jira stub
3. POST `demo/payload_malformed.json` → see guardrail reject + alarm
4. Swap agent: POST same payload with `?agent=claude` → identical harness path, different `Proposal`
5. List runs: `GET /runs`, inspect a run: `GET /runs/<id>`
