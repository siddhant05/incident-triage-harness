"""Team routing. Declarative rules in teams.yaml. First-match-wins."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .material import IncidentMaterial


@dataclass
class TeamRoute:
    team_id: str
    team_name: str
    slack: str
    pagerduty_service: str
    matched_on: list[str] = field(default_factory=list)
    unrouted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "team_id": self.team_id,
            "team_name": self.team_name,
            "slack_channel": self.slack,
            "pagerduty_service": self.pagerduty_service,
            "matched_on": self.matched_on,
            "unrouted": self.unrouted,
        }


class TeamRouter:
    def __init__(self, config_path: str | Path | None = None):
        path = Path(config_path or Path(__file__).parent / "teams.yaml")
        with open(path) as f:
            self.config = yaml.safe_load(f)

    def route(self, material: IncidentMaterial, runbook: dict[str, Any] | None) -> TeamRoute:
        """Find the owning team. Returns unrouted fallback if no match."""
        tag_set = _tag_set(material.tags)
        runbook_id = (runbook or {}).get("id")
        for team in self.config.get("teams", []):
            matched = self._match(team, material, tag_set, runbook_id)
            if matched:
                return TeamRoute(
                    team_id=team["id"],
                    team_name=team["name"],
                    slack=team["slack"],
                    pagerduty_service=team["pagerduty_service"],
                    matched_on=matched,
                )
        u = self.config.get("unrouted", {})
        return TeamRoute(
            team_id="unrouted",
            team_name="Unrouted",
            slack=u.get("slack", "#oncall-triage"),
            pagerduty_service=u.get("pagerduty_service", "incident-manager"),
            matched_on=[],
            unrouted=True,
        )

    def _match(
        self,
        team: dict[str, Any],
        material: IncidentMaterial,
        tag_set: set[str],
        runbook_id: str | None,
    ) -> list[str]:
        rules = team.get("match", {})
        reasons: list[str] = []

        for prefix in rules.get("service_prefixes", []):
            if material.service.startswith(prefix):
                reasons.append(f"service_prefix:{prefix}")
                break

        team_tags = {t.lower() for t in rules.get("tags", [])}
        overlap = tag_set & team_tags
        if overlap:
            reasons.append(f"tags:{','.join(sorted(overlap))}")

        for prefix in rules.get("path_prefixes", []):
            for f in material.stack_files:
                if f.startswith(prefix):
                    reasons.append(f"path_prefix:{prefix}")
                    break

        rb_ids = rules.get("runbook_ids", [])
        if runbook_id and runbook_id in rb_ids:
            reasons.append(f"runbook:{runbook_id}")

        return reasons


def _tag_set(tags: list[str]) -> set[str]:
    """Normalize tag list to flat lowercase set; split key:value forms."""
    out: set[str] = set()
    for t in tags:
        t = t.lower()
        out.add(t)
        if ":" in t:
            k, v = t.split(":", 1)
            out.add(k)
            out.add(v)
    return out
