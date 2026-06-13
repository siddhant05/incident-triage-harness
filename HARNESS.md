# Incident Triage Harness — Architecture

**Author:** Siddhant Mohanty · **Date:** 2026-06-12 · **Status:** Draft v1

## Problem

On-call engineers triage repetitive Sentry/Datadog alerts at 3am. Most of them are not related to the team and some are even false network alarms. Triage is inconsistent, context is scattered across Slack/Jira, and naive auto-remediation bots act on partial data. The harness governs an AI agent that does first-pass triage with declared safety bounds, explicit pass/fail checks, and human handoff when uncertain. Agent analyzes stack traces and queries git history to propose likely culprit files from recent changes.

## Flow

```
Sentry webhook → Material → Guardrails(pre) → Agent → Checkpoints → Guardrails(post) → Alarms → Slack / Jira / PagerDuty
                                                                                    ↓
                                                                                 SQLite (replayable)
```

| Pillar      | Module                              | Responsibility                                            |
| ----------- | ----------------------------------- | --------------------------------------------------------- |
| Material    | `material.py`                       | Normalize Sentry JSON → `IncidentMaterial`; redact PII    |
| Guardrails  | `guardrails.py` + `guardrails.yaml` | Declared rules; pre (input/budget) + post (action ladder) |
| Agent       | `agents/` via `AgentInterface`      | Primary: `GeminiAgent` / `ClaudeAgent` (LLM). Fallback: `HeuristicAgent` (rules-only, when LLM unavailable). Produces `Proposal`. |
| Checkpoints | `checkpoints.py` + SQLite           | Explicit pass/fail, persisted per stage                   |
| Alarms      | `alarms.py`                         | Typed `{name, severity, context, recommended_action}`     |

## Guardrails (declared in YAML)

- `input.require_fields: [event_id, service, severity]` — reject malformed
- `input.redact_patterns: [aws_key, jwt, email]` — scrub before LLM
- `budget.daily_token_cap: 500_000` — over → skip LLM, escalate
- `rate.per_service_per_min: 20` — collapse storms into incident group
- `action.ladder` — see below; agent action outside ladder → `ACTION_OUT_OF_BOUNDS`

**Action ladder** (post-guardrail check):

| Action           | Min severity | Min confidence | Notes                     |
| ---------------- | ------------ | -------------- | ------------------------- |
| Slack diagnosis  | any          | any            | always                    |
| Jira create      | P3           | 0.6            | runbook matched           |
| Page secondary   | P2           | 0.7            | runbook matched           |
| Suggest rollback | P1           | 0.8            | advisory only, never auto |
| Mark resolved    | —            | —              | human only                |

## Checkpoints (explicit pass/fail)

1. `signal_sufficiency` — required fields present, stack trace non-empty
2. `runbook_match` — at least one runbook tag intersects incident tags
3. `git_analysis` — stack trace filenames match files in recent commits (5 most recent)
4. `confidence_cross_check` — for LLM agents, harness computes a weighted signal score from `runbook_match` + `git_analysis` + `signal_sufficiency` (weights in `guardrails.yaml: confidence.components`). If `harness_signal < signal_floor`, the LLM self-reported confidence is capped at `cap_when_low_signal`. Heuristic agent is exempt — its confidence is already the signal score. Recorded as a stage and as cross-check fields on the `Proposal`.
5. `confidence_threshold` — final (post-cap) confidence ≥ action's min confidence
6. `action_safety` — proposed action permitted by ladder for severity

Fail at any stage → halt pipeline → emit alarm → escalate human. Each checkpoint's input/output persists to SQLite; run replayable from any stage.

## Agent loops

Agent executes in single pass (no multi-turn in v1). Pipeline is synchronous; no retry loops. Hard limits:

- **LLM timeout** 15s → fallback to `HeuristicAgent` (rules-only, no LLM), emit `AGENT_TIMEOUT` alarm. Heuristic is the degraded-mode backstop; primary triage is LLM.
- **Git call timeout** 5s per call (`git_log`, `git_blame`, `git_diff`) → skip analysis, emit `GIT_TIMEOUT`, continue with routing only
- **Token budget** per-incident cap 8k tokens → clip context, escalate if cap hit mid-execution
- **Proposal validation timeout** 5s → if agent returns malformed Proposal, emit `AGENT_OUTPUT_INVALID`, halt

No agent self-correction loop. On failure, harness stops and escalates human.

## Tool calls

**Agent reads (no side effects):**

- `lookup_runbook(tags)` → returns matched `Runbook` or empty
- `get_recent_deploys(service)` → mocked, returns last 3 deploys
- `get_service_health(service)` → mocked, returns status

**Agent git tools (GitHub REST API, local subprocess fallback):**

- `collect_git_context(repo_spec, service, stack_files)` → dispatches by shape:
  - `owner/repo` form (e.g. `GITHUB_REPO=psf/requests`) → GitHub REST: `GET /repos/{owner}/{repo}/commits?per_page=5`, then `GET /repos/{owner}/{repo}/commits/{sha}` per commit to pull `files[].filename`. Honors optional `GITHUB_TOKEN`. Blame is skipped (no clean REST equivalent) and returned as `[]`.
  - Local path form (e.g. `DEMO_GIT_REPO=$PWD/demo-repo`) → falls back to subprocess `git log --name-only` + `git blame --line-porcelain`.
- Errors (timeout, 404, 401, 403+rate-limit) return `{"available": False, "reason": "..."}` so the pipeline emits a `GIT_TIMEOUT` alarm and continues.
- Output schema is identical across both paths: `{"available", "repo", "recent_commits": [{sha, author, msg, files_changed}], "blame": [...]}`.

**Harness executes on Proposal (controlled by guardrails):**

| Tool           | Guardrail gate                                 | Stub in v1?       | Real in prod |
| -------------- | ---------------------------------------------- | ----------------- | ------------ |
| Slack post     | none                                           | no                | real webhook |
| Jira create    | P3+ severity, 0.6+ confidence                  | yes (console log) | API call     |
| PagerDuty page | P2+ severity, 0.7+ confidence, runbook matched | yes (console log) | API call     |
| Mark resolved  | human-only flag                                | always blocked    | never auto   |

Agent sees no output tools. Agent output `Proposal` is scored → checkpoints decide which tool harness calls.

## Evals

- **Replay corpus** — 5 real Sentry payloads from OSS repo with stack traces matching recent commits. Run pre-demo; validate git analysis finds correct suspect files.
- **Confidence calibration** — log predicted confidence vs git-analysis match.
- **Guardrail unit tests** — each YAML rule has a fixture (malformed input, oversized payload, out-of-ladder action).
- **Checkpoint contract tests** — each checkpoint must return `{pass: bool, score: float, reason: str}`.
- **Git integration test** — agent calls `git_log`/`git_blame`/`git_diff` on demo repo, returns expected format.
- **Swap test** — same payload through `ClaudeAgent` and `HeuristicAgent`; assert harness path identical, only `Proposal` differs.
- **Post-incident feedback** — on-call replies ✅/❌ in Slack thread → SQLite → threshold tuning data.

## Key decisions

- Python + FastAPI (fast webhook, Anthropic + Google GenAI SDKs), SQLite (zero ops, replayable), YAML guardrails (auditable, declared), pipeline not DAG (linear triage flow), `AgentInterface` protocol (clean swap seam), LLM agents self-report confidence and the harness cross-checks against an independent signal score (runbook + git + signal_sufficiency); when signals are weak the LLM confidence is capped — heuristic agent is exempt because its confidence IS the signal score.
- Real OSS repo for demo (not mocked git) — agent uses actual `git` subprocess on clone for authenticity.
- Swappable agents via `AgentInterface`: `gemini` / `claude` (primary LLM workers) + `heuristic` (rules-only fallback for LLM outage, budget exhaustion, or offline demo). LLM SDKs imported lazily so heuristic-only runs need no API keys.

## Tradeoffs / uncertainty

- Tag-match runbooks brittle vs embeddings — accept v1, roadmap.
- Confidence thresholds unvalidated — ship conservative, tune via feedback.
- Git analysis scope — only matches filenames, not function/line diffs (sufficient for v1).
- Git subprocess failures (e.g., repo unavailable) → graceful skip with `GIT_TIMEOUT` alarm, routing continues.
- No live Sentry enrichment — webhook payload only.
- LLM stage replay non-deterministic — documented.
- Harness observability uses Sentry (coupling risk).
- Action ladder thresholds = policy, not science.

## Non-goals (v1)

Auto-remediation. Multi-turn diagnosis. Cross-incident correlation. Multi-tenant config. Learning loop.
