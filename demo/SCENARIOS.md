# Demo Scenario Catalog

Every scenario fires through `demo/fire_sentry_event.py`. Most go through
Sentry (DSN configured in the script). Three modes (`--storm`, `--dedup`,
`--malformed`) POST directly to the deployed harness at
`https://incident-triage-harness.onrender.com/webhook/sentry?agent=gemini`.

List everything quickly:

```bash
python demo/fire_sentry_event.py --list
```

Pipeline recap (per HARNESS.md):

    material -> guardrails(pre) -> runbook lookup -> routing -> git context
              -> agent (gemini) -> confidence cross-check -> checkpoints
              -> guardrails(post) -> notifiers (Slack + Jira/PagerDuty stubs)

Action ladder (from `harness/guardrails.yaml`):

| action            | min severity | min confidence | runbook required |
|-------------------|:------------:|:--------------:|:----------------:|
| slack_post        | P4           | 0.0            | no               |
| jira_create       | P3           | 0.6            | yes              |
| page_secondary    | P2           | 0.7            | yes              |
| suggest_rollback  | P1           | 0.8            | advisory         |
| mark_resolved     | human only   | -              | -                |

---

## Pillar 1 — Material (routing + git context)

### route_ads
- **Command:** `python demo/fire_sentry_event.py --kind route_ads`
- **What happens:** P2 error from `ads-campaign-svc` with `campaign` tag.
  Router matches `ads-platform` via service prefix `campaign-` and tag.
- **Expected Slack:** posted to `#ads-oncall`, title prefixed with team
  name; action `jira_create` if runbook hits, otherwise `slack_post`.
- **Proves:** service-prefix + tag routing.

### route_checkout
- **Command:** `python demo/fire_sentry_event.py --kind route_checkout`
- **What happens:** P2 from `checkout-payment` with `stripe` + `payment`
  tags. Routes to `checkout` (`#checkout-oncall`).
- **Proves:** multi-tag routing into checkout team.

### route_identity
- **Command:** `python demo/fire_sentry_event.py --kind route_identity`
- **What happens:** P2 from `user-sso` with `auth` + `oauth`. Routes to
  `identity` (`#identity-oncall`).
- **Proves:** identity routing without runbook overlap.

### unrouted
- **Command:** `python demo/fire_sentry_event.py --kind unrouted`
- **What happens:** P2 from `totally-unknown-svc`, no tags. Router has
  no match → falls back to `unrouted` (`#oncall-triage`).
- **Expected alarm:** `ESCALATION_REQUIRED`.
- **Proves:** unrouted fallback + escalation alarm.

### no_git_signal
- **Command:** `python demo/fire_sentry_event.py --kind no_git_signal`
- **What happens:** Runbook (`null_pointer_python`) matches via tags, but
  the stack frame filename `app/notexist_anywhere.py` won't resolve in
  `psf/requests`. Git tool returns no suspect commits.
- **Expected alarm:** `GIT_ANALYSIS_INCONCLUSIVE` (soft).
- **Proves:** material gathering degrades gracefully when git signal is
  missing; harness still proceeds with reduced confidence.

---

## Pillar 2 — Guardrails (input + action ladder)

### --malformed (input guardrail)
- **Command:** `python demo/fire_sentry_event.py --malformed`
- **What happens:** POST to harness with empty `event_id`, no service tag.
  `guardrails(pre)` rejects on `require_fields` check.
- **Expected HTTP:** 400 / structured rejection JSON.
- **Expected alarm:** `INPUT_GUARDRAIL_REJECTED`.
- **Proves:** required-field enforcement at the input boundary.

### false_alarm (action-ladder floor)
- **Command:** `python demo/fire_sentry_event.py --kind false_alarm`
- **What happens:** P4 info-level transient from `ads-tracker`. Ladder
  permits `slack_post` only; `jira_create` blocked by severity floor.
- **Expected Slack:** brief FYI to `#ads-oncall`, no Jira stub, no page.
- **Proves:** low-severity events stay quiet.

### rollback_block (action ladder cap)
- **Command:** `python demo/fire_sentry_event.py --kind rollback_block`
- **What happens:** P3 warning. LLM, given runbook + clean git diff, is
  likely to propose `suggest_rollback`. Ladder requires P1 + 0.8 conf for
  rollback. Post-guardrail downgrades to `jira_create` or `slack_post`.
- **Expected alarm:** `ACTION_OUT_OF_BOUNDS`.
- **Proves:** ladder demotes over-aggressive LLM proposals.

### critical_low_conf (confidence cap)
- **Command:** `python demo/fire_sentry_event.py --kind critical_low_conf`
- **What happens:** P0 fatal from `mystery-svc`. No runbook match, no
  useful tags, generic message. Signal score < `signal_floor (0.6)`, so
  LLM confidence is capped at `cap_when_low_signal (0.4)`. Page is
  blocked (needs 0.7).
- **Expected alarm:** `CONFIDENCE_CAPPED` + `ESCALATION_REQUIRED`.
- **Expected Slack:** triage-only message to incident manager.
- **Proves:** harness overrides over-confident LLM on weak signal.

---

## Pillar 3 — Checkpoints (dedup, rate, budget)

### --dedup
- **Command:** `python demo/fire_sentry_event.py --dedup`
- **What happens:** Two identical POSTs (same `event_id`) directly to the
  webhook. First runs full pipeline; second short-circuits.
- **Expected:** second response indicates `DEDUP_HIT`.
- **Proves:** event-id dedup checkpoint.

### --storm
- **Command:** `python demo/fire_sentry_event.py --storm 22 --kind null`
- **WARNING:** This *will* create ~22 Sentry issues / pings. Use sparingly.
- **What happens:** 22 events from the same service in tight succession.
  Per-service rate is 20/min (`guardrails.yaml`).
- **Expected alarm:** `RATE_LIMIT_EXCEEDED` on the 21st+ event.
- **Proves:** per-service rate limit.

---

## Pillar 4 — Alarms (end-to-end happy + critical paths)

### null (happy path)
- **Command:** `python demo/fire_sentry_event.py --kind null`
- **What happens:** Baseline AttributeError, `users-api`. Routes
  `identity` (runbook overlap). Full pipeline runs cleanly.
- **Expected Slack:** triage post to `#identity-oncall` with runbook link.
- **Proves:** end-to-end happy path with all signals present.

### db (infra runbook)
- **Command:** `python demo/fire_sentry_event.py --kind db`
- **What happens:** P0 Postgres timeout from `checkout-svc`. Runbook
  `db_connection_timeout` matches → routes `infra-platform`.
- **Expected Slack:** `#infra-oncall`; severity + runbook give enough
  confidence for `page_secondary`.

### ads (basic routing)
- **Command:** `python demo/fire_sentry_event.py --kind ads`
- **What happens:** `ads-tracker` ValueError. Routes `ads-platform`.
- **Expected:** `slack_post` + (optional) `jira_create`.

### critical_high_conf
- **Command:** `python demo/fire_sentry_event.py --kind critical_high_conf`
- **What happens:** P0 + runbook + git frames in `src/requests/sessions.py`
  (real file in `psf/requests`). High signal → LLM confidence stands.
  Ladder permits `page_secondary`.
- **Expected Slack:** page-grade post; PagerDuty stub fires.
- **Proves:** critical incident routed and paged.

### no_runbook
- **Command:** `python demo/fire_sentry_event.py --kind no_runbook`
- **What happens:** P2 from `mystery-svc`, single obscure tag, no runbook
  match. Pipeline still routes (unrouted fallback) and produces a
  Proposal with `runbook_match=False`.
- **Expected alarm:** `NO_RUNBOOK_MATCH` (soft, advisory).
- **Proves:** missing runbook surfaces as alarm, not failure.

---

## Demo script (5 min)

Suggested sequence to walk a reviewer through the harness end to end:

1. **Happy path** — `python demo/fire_sentry_event.py --kind null`
   - Show pipeline produces a clean Slack post + Jira stub.
   - Open `/runs` in the harness UI; click into the run.

2. **Critical, high signal** — `--kind critical_high_conf`
   - Same pipeline, now `page_secondary` fires. Point out runbook +
     git-suspect link.

3. **Confidence cap** — `--kind critical_low_conf`
   - Same severity, *no* signal. Harness caps LLM confidence; page is
     blocked; `CONFIDENCE_CAPPED` alarm in the run detail.

4. **Action ladder block** — `--kind rollback_block`
   - LLM wants to suggest rollback. Ladder demotes it.
     `ACTION_OUT_OF_BOUNDS` alarm visible.

5. **Routing variety** — fire `route_ads`, `route_checkout`,
   `route_identity` back to back; show three different Slack channels
   in `/runs`.

6. **Dedup** — `--dedup`
   - Two POSTs, second one short-circuits.

7. **Storm (optional, noisy)** — `--storm 22 --kind null`
   - Show `RATE_LIMIT_EXCEEDED` alarm after the 20th event.

Bail-out demos if time is short: pick (1), (3), (4), (6).
