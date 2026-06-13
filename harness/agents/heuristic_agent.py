"""Rules-based agent. No LLM. Proves harness is not an LLM wrapper."""
from __future__ import annotations

from typing import Any

from ..material import IncidentMaterial
from .base import Proposal


SEVERITY_TO_ACTION = {
    "P0": "page_secondary",
    "P1": "page_secondary",
    "P2": "page_secondary",
    "P3": "jira_create",
    "P4": "slack_post",
}


class HeuristicAgent:
    name = "heuristic"

    def execute(
        self,
        material: IncidentMaterial,
        runbook: dict[str, Any] | None,
        git_context: dict[str, Any],
    ) -> Proposal:
        recent_commits = git_context.get("recent_commits") or []
        stack_files = set(material.stack_files)
        suspect_commits = []
        suspect_files = []
        for commit in recent_commits:
            files_changed = set(commit.get("files_changed") or [])
            overlap = stack_files & files_changed
            if overlap:
                suspect_commits.append(commit)
                suspect_files.extend(overlap)

        runbook_matched = runbook is not None
        git_signal = len(suspect_commits) > 0

        # Confidence heuristic
        confidence = 0.3
        if runbook_matched:
            confidence += 0.3
        if git_signal:
            confidence += 0.3
        confidence = min(confidence, 0.95)

        # If low signal, downgrade action
        action = SEVERITY_TO_ACTION.get(material.severity, "slack_post")
        if not runbook_matched and not git_signal:
            action = "slack_post"
            confidence = 0.2

        hypothesis_parts = []
        if runbook_matched:
            hypothesis_parts.append(f"matches runbook '{runbook.get('id')}'")
        if git_signal:
            shas = ", ".join(c.get("sha", "")[:8] for c in suspect_commits[:3])
            hypothesis_parts.append(f"recent commits touch stack files: {shas}")
        if not hypothesis_parts:
            hypothesis_parts.append("no runbook or git signal; routing for human review")

        return Proposal(
            hypothesis="; ".join(hypothesis_parts),
            recommended_action=action,
            confidence=round(confidence, 2),
            routing_team=(runbook or {}).get("team"),
            suspect_files=list(dict.fromkeys(suspect_files)),
            suspect_commits=suspect_commits[:3],
            runbook_id=(runbook or {}).get("id"),
            raw_agent_output={"engine": "heuristic"},
            tokens_used=0,
        )
