# Incident Triage Harness — Architecture

**Author:** Siddhant Mohanty · **Date:** 2026-06-12 · **Status:** Draft v1

## Problem

On-call engineers triage repetitive Sentry/Datadog alerts at 3am. Most of them are not related to the team and some are even false network alarms. Triage is inconsistent, context is scattered across Slack/Jira, and naive auto-remediation bots act on partial data. The harness governs an AI agent that does first-pass triage with declared safety bounds, explicit pass/fail checks, and human handoff when uncertain.

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
| Agent       | `agents/` via `AgentInterface`      | `ClaudeAgent` or `HeuristicAgent`; produces `Proposal`    |
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
3. `confidence_threshold` — derived score ≥ action's min confidence
4. `action_safety` — proposed action permitted by ladder for severity

Fail at any stage → halt pipeline → emit alarm → escalate human. Each checkpoint's input/output persists to SQLite; run replayable from any stage.

## Evals

- **Replay corpus** — 20 historical Sentry payloads (sanitized) with engineer-labeled correct triage. Run pre-deploy; track precision/recall on runbook match and action recommendation.
- **Confidence calibration** — log predicted confidence vs label correctness; report Brier score.
- **Guardrail unit tests** — each YAML rule has a fixture (malformed input, oversized payload, out-of-ladder action).
- **Checkpoint contract tests** — each checkpoint must return `{pass: bool, score: float, reason: str}`.
- **Swap test** — same payload through `ClaudeAgent` and `HeuristicAgent`; assert harness path identical, only `Proposal` differs.
- **Post-incident feedback** — on-call replies ✅/❌ in Slack thread → SQLite → threshold tuning data.

## Key decisions

- Python + FastAPI (fast webhook, Anthropic SDK), SQLite (zero ops, replayable), YAML guardrails (auditable, declared), pipeline not DAG (linear triage flow), `AgentInterface` protocol (clean swap seam), checkpoint-derived confidence (not LLM self-report).

## Tradeoffs / uncertainty

- Tag-match runbooks brittle vs embeddings — accept v1, roadmap.
- Confidence thresholds unvalidated — ship conservative, tune via feedback.
- No live Sentry enrichment — webhook payload only.
- LLM stage replay non-deterministic — documented.
- Harness observability uses Sentry (coupling risk).
- Action ladder thresholds = policy, not science.

## Non-goals (v1)

Auto-remediation. Multi-turn diagnosis. Cross-incident correlation. Multi-tenant config. Learning loop.
