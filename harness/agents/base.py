"""Swappable agent contract. Single execute() method."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..material import IncidentMaterial


@dataclass
class Proposal:
    """Agent output. Scored by checkpoints, gated by guardrails."""

    hypothesis: str
    recommended_action: str  # one of action_ladder keys
    confidence: float  # 0..1
    routing_team: str | None = None
    suspect_files: list[str] = field(default_factory=list)
    suspect_commits: list[dict[str, Any]] = field(default_factory=list)
    runbook_id: str | None = None
    raw_agent_output: dict[str, Any] = field(default_factory=dict)
    tokens_used: int = 0
    # Harness-derived confidence cross-check fields (populated by pipeline, not the agent).
    # `confidence` above is the FINAL value (post-cap); `llm_reported_confidence` is the raw self-report.
    llm_reported_confidence: float | None = None
    harness_signal_score: float | None = None
    confidence_components: dict[str, float] | None = None
    confidence_cap_applied: bool = False
    confidence_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis,
            "recommended_action": self.recommended_action,
            "confidence": self.confidence,
            "routing_team": self.routing_team,
            "suspect_files": self.suspect_files,
            "suspect_commits": self.suspect_commits,
            "runbook_id": self.runbook_id,
            "tokens_used": self.tokens_used,
            "llm_reported_confidence": self.llm_reported_confidence,
            "harness_signal_score": self.harness_signal_score,
            "confidence_components": self.confidence_components,
            "confidence_cap_applied": self.confidence_cap_applied,
            "confidence_reason": self.confidence_reason,
        }


class AgentInterface(Protocol):
    """Drop-in contract. Any agent implementing this swaps without harness change."""

    name: str

    def execute(
        self,
        material: IncidentMaterial,
        runbook: dict[str, Any] | None,
        git_context: dict[str, Any],
    ) -> Proposal: ...
