"""Typed alarms. {name, severity, context, recommended_action}."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AlarmSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# Declared alarm catalog. Adding an alarm = add a name here.
ALARM_CATALOG: dict[str, dict[str, Any]] = {
    "INPUT_INVALID": {
        "severity": AlarmSeverity.HIGH,
        "action": "reject_and_log",
    },
    "RATE_LIMIT_EXCEEDED": {
        "severity": AlarmSeverity.MEDIUM,
        "action": "collapse_to_incident_group",
    },
    "BUDGET_EXCEEDED": {
        "severity": AlarmSeverity.HIGH,
        "action": "skip_llm_route_to_heuristic",
    },
    "NO_RUNBOOK_MATCH": {
        "severity": AlarmSeverity.HIGH,
        "action": "escalate_to_human_with_context",
    },
    "AGENT_TIMEOUT": {
        "severity": AlarmSeverity.HIGH,
        "action": "fallback_to_heuristic_agent",
    },
    "AGENT_OUTPUT_INVALID": {
        "severity": AlarmSeverity.HIGH,
        "action": "halt_and_escalate",
    },
    "GIT_TIMEOUT": {
        "severity": AlarmSeverity.LOW,
        "action": "skip_git_analysis_continue",
    },
    "GIT_ANALYSIS_INCONCLUSIVE": {
        "severity": AlarmSeverity.LOW,
        "action": "post_anyway_flag_uncertainty",
    },
    "ACTION_OUT_OF_BOUNDS": {
        "severity": AlarmSeverity.CRITICAL,
        "action": "block_action_escalate_security",
    },
    "LOW_CONFIDENCE": {
        "severity": AlarmSeverity.MEDIUM,
        "action": "downgrade_action_or_escalate",
    },
    "ESCALATION_REQUIRED": {
        "severity": AlarmSeverity.HIGH,
        "action": "page_human_oncall",
    },
}


@dataclass
class Alarm:
    name: str
    severity: AlarmSeverity
    context: dict[str, Any] = field(default_factory=dict)
    recommended_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "context": self.context,
            "recommended_action": self.recommended_action,
        }


def emit(name: str, context: dict[str, Any] | None = None) -> Alarm:
    if name not in ALARM_CATALOG:
        raise ValueError(f"unknown alarm: {name}")
    meta = ALARM_CATALOG[name]
    return Alarm(
        name=name,
        severity=meta["severity"],
        context=context or {},
        recommended_action=meta["action"],
    )
