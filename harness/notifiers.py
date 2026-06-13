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
    username: str | None = None,
    icon_emoji: str | None = None,
) -> dict[str, Any]:
    """Post to Slack webhook. Returns {sent, status, channel}. Falls back to log if no webhook.

    Pass `username` and `icon_emoji` to break Slack's consecutive-message header consolidation
    so each scenario renders as its own row.
    """
    url = os.environ.get("SLACK_WEBHOOK_URL")
    payload: dict[str, Any] = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    if username:
        payload["username"] = username
    if icon_emoji:
        payload["icon_emoji"] = icon_emoji
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


def slack_identity(team_id: str | None, status: str, severity: str | None) -> tuple[str, str]:
    """Choose username + icon_emoji per scenario so Slack doesn't consolidate headers."""
    sev = (severity or "").upper()
    if status == "escalated":
        emoji = ":rotating_light:"
    elif status == "rejected":
        emoji = ":no_entry_sign:"
    elif status == "cached":
        emoji = ":package:"
    elif sev in {"P0", "P1"}:
        emoji = ":fire:"
    elif sev == "P2":
        emoji = ":pager:"
    elif sev == "P3":
        emoji = ":ticket:"
    else:
        emoji = ":speech_balloon:"
    team_label = (team_id or "triage").replace("_", "-")
    username = f"Triage [{team_label}]"
    return username, emoji


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
    harness_signal = proposal.get("harness_signal_score")
    llm_reported = proposal.get("llm_reported_confidence")
    if harness_signal is not None and llm_reported is not None:
        final = proposal.get("confidence", 0)
        cap_note = " (capped)" if proposal.get("confidence_cap_applied") else ""
        lines.append(
            f"*Confidence breakdown:* LLM={float(llm_reported):.2f} "
            f"harness={float(harness_signal):.2f} → final={float(final):.2f}{cap_note}"
        )
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
