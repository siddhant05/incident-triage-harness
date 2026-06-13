#!/usr/bin/env python3
"""Fire test events into Sentry (or directly into the harness) to exercise
every guardrail / checkpoint / alarm in the Incident Triage Harness.

Setup once:
    pip install sentry-sdk requests

Common runs:
    python demo/fire_sentry_event.py --list
    python demo/fire_sentry_event.py --kind null
    python demo/fire_sentry_event.py --kind critical_high_conf
    python demo/fire_sentry_event.py --kind rollback_block
    python demo/fire_sentry_event.py --storm 22 --kind null     # rate-limit demo
    python demo/fire_sentry_event.py --dedup                    # dedup demo
    python demo/fire_sentry_event.py --malformed                # input-guardrail demo

The --storm / --dedup / --malformed modes either spam Sentry or POST
directly to the harness (no Sentry round trip). See demo/SCENARIOS.md for
full catalog.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from typing import Callable

import sentry_sdk

DSN = "https://122c72f5be399f756d9bf5fa02ad55e1@o4511559224459264.ingest.us.sentry.io/4511559237238784"
HARNESS_URL = "https://incident-triage-harness.onrender.com"


# ---------------------------------------------------------------------------
# Helpers to control the Sentry stack frames Sentry will report.
# We compile a tiny module whose __file__ is whatever we want so that the
# raised exception's frame shows the synthetic path. This lets us simulate
# stack frames in src/requests/sessions.py or app/notexist_anywhere.py.
# ---------------------------------------------------------------------------
def _raise_from_synthetic_file(filename: str, func_name: str, exc_type: str, message: str) -> None:
    src = (
        f"def {func_name}():\n"
        f"    raise {exc_type}({message!r})\n"
        f"{func_name}()\n"
    )
    code = compile(src, filename, "exec")
    exec(code, {"__name__": filename.replace("/", "_").replace(".", "_")})


def _capture_with_synthetic_frame(filename: str, func_name: str, exc_type: str, message: str) -> None:
    try:
        _raise_from_synthetic_file(filename, func_name, exc_type, message)
    except Exception:
        sentry_sdk.capture_exception()


def _force_unique_grouping(kind: str) -> None:
    """Sentry collapses similar events into one issue (one webhook).
    Tag + fingerprint with a UUID so each fire becomes its own Sentry issue.
    """
    unique = uuid.uuid4().hex[:8]
    sentry_sdk.set_tag("scenario", kind)
    sentry_sdk.set_tag("fire_id", unique)
    sentry_sdk.set_context("scenario", {"kind": kind, "fire_id": unique})
    # Custom fingerprint -> new Sentry issue every call.
    try:
        sentry_sdk.get_isolation_scope().set_fingerprint(["scenario", kind, unique])
    except Exception:
        try:
            sentry_sdk.set_fingerprint(["scenario", kind, unique])  # older SDK
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Existing kinds (kept verbatim for back-compat)
# ---------------------------------------------------------------------------
def fire_null_pointer():
    sentry_sdk.set_tag("service", "users-api")
    sentry_sdk.set_tag("python", "true")
    sentry_sdk.set_tag("attribute_error", "true")
    try:
        x = None
        x.name  # noqa
    except Exception:
        sentry_sdk.capture_exception()


def fire_db_timeout():
    sentry_sdk.set_tag("service", "checkout-svc")
    sentry_sdk.set_tag("database", "true")
    sentry_sdk.set_tag("postgres", "true")
    sentry_sdk.set_tag("timeout", "true")
    sentry_sdk.set_level("fatal")
    try:
        raise TimeoutError("OperationalError: timeout expired (postgres)")
    except Exception:
        sentry_sdk.capture_exception()


def fire_ads_error():
    sentry_sdk.set_tag("service", "ads-tracker")
    sentry_sdk.set_tag("ads", "true")
    sentry_sdk.set_tag("campaign", "cpc")
    try:
        raise ValueError("invalid campaign_id format")
    except Exception:
        sentry_sdk.capture_exception()


# ---------------------------------------------------------------------------
# New scenarios
# ---------------------------------------------------------------------------
def fire_critical_high_conf():
    """P0 + clean runbook + clean git signal -> page_secondary path."""
    sentry_sdk.set_tag("service", "users-api")
    sentry_sdk.set_tag("python", "true")
    sentry_sdk.set_tag("attribute_error", "true")
    sentry_sdk.set_tag("session_corruption", "true")
    sentry_sdk.set_level("fatal")
    _capture_with_synthetic_frame(
        "src/requests/sessions.py", "send", "AttributeError",
        "'NoneType' object has no attribute 'name'",
    )


def fire_critical_low_conf():
    """P0 but vague: unknown service, no runbook overlap, no useful tags."""
    sentry_sdk.set_tag("service", "mystery-svc")
    sentry_sdk.set_level("fatal")
    try:
        raise RuntimeError("something blew up but we don't know what")
    except Exception:
        sentry_sdk.capture_exception()


def fire_false_alarm():
    """P4 / transient. Should never page; slack_post only."""
    sentry_sdk.set_tag("service", "ads-tracker")
    sentry_sdk.set_tag("transient", "true")
    sentry_sdk.set_tag("network", "true")
    sentry_sdk.set_level("info")
    try:
        raise ConnectionResetError("transient socket reset, retried OK")
    except Exception:
        sentry_sdk.capture_exception()


def fire_rollback_block():
    """P3 -- LLM may want suggest_rollback but ladder requires P1 -> blocked."""
    sentry_sdk.set_tag("service", "users-api")
    sentry_sdk.set_tag("python", "true")
    sentry_sdk.set_tag("attribute_error", "true")
    sentry_sdk.set_level("warning")
    _capture_with_synthetic_frame(
        "src/requests/sessions.py", "send", "AttributeError",
        "'NoneType' object has no attribute 'name' (post-deploy regression)",
    )


def fire_no_runbook():
    """P2 with tag that matches no runbook -> NO_RUNBOOK_MATCH soft alarm."""
    sentry_sdk.set_tag("service", "mystery-svc")
    sentry_sdk.set_tag("obscure_unmatched", "true")
    sentry_sdk.set_level("error")
    try:
        raise RuntimeError("obscure_unmatched failure mode -- no runbook for this")
    except Exception:
        sentry_sdk.capture_exception()


def fire_unrouted():
    """P2 service+tags that match no team -> unrouted, ESCALATION_REQUIRED."""
    sentry_sdk.set_tag("service", "totally-unknown-svc")
    sentry_sdk.set_level("error")
    try:
        raise RuntimeError("unknown subsystem failure")
    except Exception:
        sentry_sdk.capture_exception()


def fire_no_git_signal():
    """Runbook hits but stack frames point at files that won't exist in git."""
    sentry_sdk.set_tag("service", "users-api")
    sentry_sdk.set_tag("python", "true")
    sentry_sdk.set_tag("attribute_error", "true")
    sentry_sdk.set_level("error")
    _capture_with_synthetic_frame(
        "app/notexist_anywhere.py", "handle", "AttributeError",
        "'NoneType' object has no attribute 'name'",
    )


def fire_route_ads():
    sentry_sdk.set_tag("service", "ads-campaign-svc")
    sentry_sdk.set_tag("campaign", "display")
    sentry_sdk.set_level("error")
    try:
        raise ValueError("campaign budget overflow")
    except Exception:
        sentry_sdk.capture_exception()


def fire_route_checkout():
    sentry_sdk.set_tag("service", "checkout-payment")
    sentry_sdk.set_tag("stripe", "true")
    sentry_sdk.set_tag("payment", "true")
    sentry_sdk.set_level("error")
    try:
        raise RuntimeError("stripe charge declined: card_error")
    except Exception:
        sentry_sdk.capture_exception()


def fire_route_identity():
    sentry_sdk.set_tag("service", "user-sso")
    sentry_sdk.set_tag("auth", "true")
    sentry_sdk.set_tag("oauth", "true")
    sentry_sdk.set_level("error")
    try:
        raise RuntimeError("oauth token exchange failed: invalid_grant")
    except Exception:
        sentry_sdk.capture_exception()


KINDS: dict[str, Callable[[], None]] = {
    # original
    "null": fire_null_pointer,
    "db": fire_db_timeout,
    "ads": fire_ads_error,
    # new
    "critical_high_conf": fire_critical_high_conf,
    "critical_low_conf": fire_critical_low_conf,
    "false_alarm": fire_false_alarm,
    "rollback_block": fire_rollback_block,
    "no_runbook": fire_no_runbook,
    "unrouted": fire_unrouted,
    "no_git_signal": fire_no_git_signal,
    "route_ads": fire_route_ads,
    "route_checkout": fire_route_checkout,
    "route_identity": fire_route_identity,
}

KIND_DESCRIPTIONS = {
    "null": "Baseline P2 AttributeError in users-api (happy path).",
    "db": "P0 Postgres timeout in checkout-svc (routes infra-platform via runbook).",
    "ads": "P2 ads-tracker ValueError (routes ads-platform).",
    "critical_high_conf": "P0 + runbook + git signal -> page_secondary, on-call paged.",
    "critical_low_conf": "P0 but vague -> harness CAPS LLM confidence -> escalation.",
    "false_alarm": "P4 transient -> slack_post only, no Jira, no page.",
    "rollback_block": "P3 advisory rollback request -> ACTION_OUT_OF_BOUNDS (needs P1).",
    "no_runbook": "P2 with no runbook match -> NO_RUNBOOK_MATCH soft alarm.",
    "unrouted": "P2 service that matches no team -> ESCALATION_REQUIRED, #oncall-triage.",
    "no_git_signal": "Runbook hits but stack frames not in repo -> GIT_ANALYSIS_INCONCLUSIVE.",
    "route_ads": "P2 routed cleanly to ads-platform.",
    "route_checkout": "P2 routed cleanly to checkout.",
    "route_identity": "P2 routed cleanly to identity.",
}


# ---------------------------------------------------------------------------
# Direct-to-harness modes (bypass Sentry)
# ---------------------------------------------------------------------------
def _post_to_harness(payload: dict, agent: str = "gemini") -> tuple[int, str]:
    import requests  # local import; sentry_sdk is the only hard dep
    url = f"{HARNESS_URL}/webhook/sentry?agent={agent}"
    resp = requests.post(url, json=payload, timeout=30)
    return resp.status_code, resp.text[:500]


def _canned_payload(event_id: str) -> dict:
    return {
        "event": {
            "event_id": event_id,
            "title": "AttributeError: 'NoneType' object has no attribute 'name'",
            "message": "Dedup demo event",
            "level": "error",
            "tags": [
                ["service", "users-api"],
                ["level", "error"],
                ["python", "true"],
                ["attribute_error", "true"],
            ],
            "exception": {
                "values": [
                    {
                        "type": "AttributeError",
                        "value": "'NoneType' object has no attribute 'name'",
                        "stacktrace": {
                            "frames": [
                                {"filename": "src/requests/sessions.py",
                                 "function": "send", "lineno": 712},
                            ]
                        },
                    }
                ]
            },
        }
    }


def run_dedup(agent: str) -> int:
    """POST the same event_id twice -> second hit should be DEDUP."""
    event_id = f"dedup-{uuid.uuid4().hex[:12]}"
    payload = _canned_payload(event_id)
    print(f"dedup: event_id={event_id}")
    for i in (1, 2):
        code, body = _post_to_harness(payload, agent=agent)
        print(f"  attempt {i}: HTTP {code} :: {body[:160]}")
    return 0


def run_malformed(agent: str) -> int:
    """POST a payload missing required fields (no event_id, no service tag)."""
    payload = {"event": {"event_id": "", "title": "no service tag, no severity", "tags": []}}
    code, body = _post_to_harness(payload, agent=agent)
    print(f"malformed: HTTP {code} :: {body[:200]}")
    return 0


def run_storm(kind: str, n: int) -> int:
    """Fire the same Sentry kind N times in tight succession."""
    print(f"STORM: firing {n} '{kind}' events to Sentry. This WILL create Sentry issues / pings.")
    sentry_sdk.init(dsn=DSN, send_default_pii=True, traces_sample_rate=0.0)
    fn = KINDS[kind]
    for i in range(n):
        with sentry_sdk.push_scope():
            _force_unique_grouping(kind)
            fn()
        if (i + 1) % 5 == 0:
            print(f"  fired {i + 1}/{n}")
    sentry_sdk.flush(timeout=10)
    print(f"storm complete: {n} events of kind={kind}")
    return 0


def print_list() -> int:
    print("Available --kind values (fired via Sentry SDK):\n")
    width = max(len(k) for k in KINDS) + 2
    for k in KINDS:
        desc = KIND_DESCRIPTIONS.get(k, "")
        print(f"  {k.ljust(width)}{desc}")
    print("\nDirect-to-harness modes (bypass Sentry):")
    print("  --storm N --kind K   fire N copies of kind K through Sentry (rate-limit demo)")
    print("  --dedup              POST same event_id twice (dedup checkpoint)")
    print("  --malformed          POST invalid payload (input guardrail rejects)")
    print("\nSee demo/SCENARIOS.md for the full catalog + expected Slack/alarm output.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Fire demo events at the Incident Triage Harness.")
    ap.add_argument("--kind", default="null", choices=list(KINDS), help="event flavor")
    ap.add_argument("--list", action="store_true", help="list all scenarios and exit")
    ap.add_argument("--storm", type=int, metavar="N",
                    help="fire N events of --kind in succession (default 22) to trip RATE_LIMIT_EXCEEDED")
    ap.add_argument("--dedup", action="store_true",
                    help="POST same event_id twice directly to harness (dedup demo)")
    ap.add_argument("--malformed", action="store_true",
                    help="POST malformed payload directly to harness (input guardrail demo)")
    ap.add_argument("--agent", default="gemini", help="agent query param for direct-POST modes")
    args = ap.parse_args()

    if args.list:
        return print_list()

    if args.malformed:
        return run_malformed(args.agent)

    if args.dedup:
        return run_dedup(args.agent)

    if args.storm is not None:
        n = args.storm if args.storm > 0 else 22
        return run_storm(args.kind, n)

    # Default: single Sentry event
    sentry_sdk.init(dsn=DSN, send_default_pii=True, traces_sample_rate=0.0)
    _force_unique_grouping(args.kind)
    KINDS[args.kind]()
    sentry_sdk.flush(timeout=5)
    print(f"sent: {args.kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
