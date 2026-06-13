"""Declared guardrails. Pre-agent (input/budget/rate) + post-agent (action ladder)."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .material import IncidentMaterial


SEVERITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}


@dataclass
class GuardrailResult:
    passed: bool
    rule: str
    reason: str
    details: dict[str, Any]


class GuardrailEngine:
    """Loads YAML rules. Evaluates pre and post checks."""

    def __init__(self, config_path: str | Path | None = None):
        path = Path(config_path or Path(__file__).parent / "guardrails.yaml")
        with open(path) as f:
            self.config = yaml.safe_load(f)
        self._rate_buckets: dict[str, deque[float]] = defaultdict(deque)
        self._daily_tokens = 0
        self._daily_window_start = time.time()

    # ----- pre-agent -----

    def check_input(self, material: IncidentMaterial) -> list[GuardrailResult]:
        results = []
        required = self.config["input"]["require_fields"]
        for field in required:
            value = getattr(material, field, None)
            ok = bool(value) and value != "unknown"
            results.append(
                GuardrailResult(
                    passed=ok,
                    rule=f"input.require_fields.{field}",
                    reason="present" if ok else f"missing or default value for {field}",
                    details={"value": value},
                )
            )
        return results

    def check_rate(self, service: str) -> GuardrailResult:
        cap = self.config["rate"]["per_service_per_min"]
        now = time.time()
        bucket = self._rate_buckets[service]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        bucket.append(now)
        ok = len(bucket) <= cap
        return GuardrailResult(
            passed=ok,
            rule="rate.per_service_per_min",
            reason=f"{len(bucket)} alerts in last 60s (cap {cap})",
            details={"service": service, "count": len(bucket), "cap": cap},
        )

    def check_budget(self, tokens_about_to_spend: int) -> GuardrailResult:
        now = time.time()
        if now - self._daily_window_start > 86400:
            self._daily_tokens = 0
            self._daily_window_start = now
        cap = self.config["budget"]["daily_token_cap"]
        projected = self._daily_tokens + tokens_about_to_spend
        ok = projected <= cap
        return GuardrailResult(
            passed=ok,
            rule="budget.daily_token_cap",
            reason=f"projected {projected}/{cap}",
            details={"daily_spent": self._daily_tokens, "cap": cap, "projected": projected},
        )

    def record_token_spend(self, tokens: int) -> None:
        self._daily_tokens += tokens

    # ----- post-agent -----

    def check_action(
        self, action: str, severity: str, confidence: float, runbook_matched: bool
    ) -> GuardrailResult:
        ladder = self.config["action_ladder"]
        rules = ladder.get(action)
        if rules is None:
            return GuardrailResult(
                passed=False,
                rule="action_ladder.unknown",
                reason=f"action '{action}' not in declared ladder",
                details={"action": action},
            )
        if rules.get("human_only"):
            return GuardrailResult(
                passed=False,
                rule=f"action_ladder.{action}.human_only",
                reason="action reserved for humans, agent cannot execute",
                details={"action": action},
            )
        min_sev = rules.get("min_severity", "P4")
        if SEVERITY_RANK[severity] > SEVERITY_RANK[min_sev]:
            return GuardrailResult(
                passed=False,
                rule=f"action_ladder.{action}.min_severity",
                reason=f"severity {severity} below required {min_sev}",
                details={"action": action, "severity": severity, "required": min_sev},
            )
        min_conf = rules.get("min_confidence", 0.0)
        if confidence < min_conf:
            return GuardrailResult(
                passed=False,
                rule=f"action_ladder.{action}.min_confidence",
                reason=f"confidence {confidence:.2f} below {min_conf}",
                details={"action": action, "confidence": confidence, "required": min_conf},
            )
        if rules.get("require_runbook_match") and not runbook_matched:
            return GuardrailResult(
                passed=False,
                rule=f"action_ladder.{action}.require_runbook_match",
                reason="action requires runbook match",
                details={"action": action},
            )
        return GuardrailResult(
            passed=True,
            rule=f"action_ladder.{action}",
            reason="action permitted",
            details={"action": action, "advisory_only": rules.get("advisory_only", False)},
        )
