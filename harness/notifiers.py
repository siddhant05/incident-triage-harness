"""Output side-effects. Slack real, Jira/PagerDuty stubbed."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import requests

log = logging.getLogger("harness.notifiers")


def post_slack(
    text: str,
    blocks: list[dict[str, Any]] | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Post to Slack webhook. Returns {sent, status, channel}. Falls back to log if no webhook.

    The `channel` is recorded for visibility (team routing). Incoming webhooks bind to a
    specific channel by design, so `channel` is metadata, not a runtime override.
    """
    url = os.environ.get("SLACK_WEBHOOK_URL")
    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    result: dict[str, Any] = {"channel_intent": channel}
    if not url:
        log.info("SLACK (no webhook configured) [%s]: %s", channel, text)
        result.update({"sent": False, "status": "no_webhook_configured", "payload": payload})
        return result
    try:
        r = requests.post(url, json=payload, timeout=5)
        result.update({"sent": r.status_code == 200, "status": r.status_code, "payload": payload})
        return result
    except Exception as e:
        log.exception("slack post failed")
        result.update({"sent": False, "status": f"error:{e}", "payload": payload})
        return result


def create_jira(title: str, body: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """Stubbed. Logs what would be created."""
    record = {
        "would_create_jira": True,
        "title": title,
        "body": body,
        "fields": fields or {},
    }
    log.info("JIRA STUB: %s", json.dumps(record))
    return record


def page_pagerduty(summary: str, severity: str, context: dict[str, Any]) -> dict[str, Any]:
    """Stubbed. Logs what would be paged."""
    record = {
        "would_page_pagerduty": True,
        "summary": summary,
        "severity": severity,
        "context": context,
    }
    log.info("PAGERDUTY STUB: %s", json.dumps(record))
    return record


def format_slack_message(
    proposal: dict[str, Any],
    material: dict[str, Any],
    alarms: list[dict[str, Any]],
    route: dict[str, Any] | None = None,
) -> str:
    """Compose Slack text for diagnosis or escalation."""
    lines = [
        f"*Incident triage* — `{material.get('event_id', 'unknown')}` ({material.get('severity', '?')})",
        f"Service: `{material.get('service', '?')}`",
        f"Title: {material.get('title', '')}",
    ]
    if route:
        flag = " ⚠️ UNROUTED" if route.get("unrouted") else ""
        lines.append(
            f"Routed to: *{route.get('team_name', '?')}* (`{route.get('team_id','?')}`) "
            f"→ {route.get('slack_channel','?')}{flag}"
        )
        if route.get("matched_on"):
            lines.append(f"  matched_on: {', '.join(route['matched_on'])}")
    lines.extend([
        "",
        f"*Hypothesis:* {proposal.get('hypothesis', '(none)')}",
        f"*Confidence:* {proposal.get('confidence', 0):.2f}",
        f"*Recommended action:* `{proposal.get('recommended_action', '?')}`",
    ])
    suspects = proposal.get("suspect_commits") or []
    if suspects:
        lines.append("")
        lines.append("*Suspect commits:*")
        for c in suspects[:3]:
            lines.append(f"  • `{(c.get('sha') or '')[:8]}` {c.get('msg','')[:80]} — {c.get('author','')}")
    if alarms:
        lines.append("")
        lines.append("*Alarms:*")
        for a in alarms:
            lines.append(f"  • `{a['name']}` ({a['severity']}) → {a['recommended_action']}")
    return "\n".join(lines)
