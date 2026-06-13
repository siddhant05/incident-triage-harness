"""Explicit pass/fail checkpoints. Each returns {pass, score, reason}."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agents.base import Proposal
from .material import IncidentMaterial


@dataclass
class CheckpointResult:
    name: str
    passed: bool
    score: float
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": self.score,
            "reason": self.reason,
            "details": self.details,
        }


def signal_sufficiency(material: IncidentMaterial) -> CheckpointResult:
    has_id = bool(material.event_id) and material.event_id != "unknown"
    has_service = bool(material.service) and material.service != "unknown"
    has_severity = material.severity in {"P0", "P1", "P2", "P3", "P4"}
    has_frames = len(material.stack_frames) > 0
    score = sum([has_id, has_service, has_severity, has_frames]) / 4.0
    passed = score >= 0.75
    return CheckpointResult(
        name="signal_sufficiency",
        passed=passed,
        score=score,
        reason=f"score={score:.2f} (id={has_id} service={has_service} sev={has_severity} frames={has_frames})",
        details={
            "has_event_id": has_id,
            "has_service": has_service,
            "has_severity": has_severity,
            "has_stack_frames": has_frames,
        },
    )


def runbook_match(runbook: dict[str, Any] | None) -> CheckpointResult:
    matched = runbook is not None
    return CheckpointResult(
        name="runbook_match",
        passed=matched,
        score=1.0 if matched else 0.0,
        reason=f"runbook={runbook.get('id') if matched else 'none'}",
        details={"runbook_id": (runbook or {}).get("id")},
    )


def git_analysis(material: IncidentMaterial, git_context: dict[str, Any]) -> CheckpointResult:
    if not git_context.get("available"):
        return CheckpointResult(
            name="git_analysis",
            passed=False,
            score=0.0,
            reason=f"git not available: {git_context.get('reason', 'unknown')}",
            details={"available": False},
        )
    stack_files = set(material.stack_files)
    recent = git_context.get("recent_commits") or []
    matched_commits = []
    for commit in recent:
        files_changed = set(commit.get("files_changed") or [])
        if stack_files & files_changed:
            matched_commits.append(commit.get("sha"))
    score = min(len(matched_commits) / 3.0, 1.0) if matched_commits else 0.0
    passed = score > 0.0
    return CheckpointResult(
        name="git_analysis",
        passed=passed,
        score=score,
        reason=f"{len(matched_commits)} recent commits overlap stack files",
        details={"matched_commit_shas": matched_commits, "stack_files": list(stack_files)},
    )


def confidence_threshold(proposal: Proposal, min_required: float) -> CheckpointResult:
    passed = proposal.confidence >= min_required
    return CheckpointResult(
        name="confidence_threshold",
        passed=passed,
        score=proposal.confidence,
        reason=f"agent confidence {proposal.confidence:.2f} vs required {min_required:.2f}",
        details={"confidence": proposal.confidence, "required": min_required},
    )


def action_safety(proposal: Proposal, guardrail_result: dict[str, Any]) -> CheckpointResult:
    passed = guardrail_result.get("passed", False)
    return CheckpointResult(
        name="action_safety",
        passed=passed,
        score=1.0 if passed else 0.0,
        reason=guardrail_result.get("reason", ""),
        details=guardrail_result,
    )
