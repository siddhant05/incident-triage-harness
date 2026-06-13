"""Pipeline orchestrator. Wires material → guardrails(pre) → agent → checkpoints → guardrails(post) → alarms → notifiers."""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from . import alarms as alarms_mod
from . import checkpoints as ck
from . import confidence as conf_mod
from . import notifiers
from . import observability
from . import tools
from .agents.base import AgentInterface, Proposal
from .guardrails import GuardrailEngine
from .material import IncidentMaterial, parse_sentry_payload
from .persistence import Store
from .routing import TeamRoute, TeamRouter

log = logging.getLogger("harness.pipeline")


@dataclass
class RunResult:
    run_id: str
    status: str  # ok | escalated | rejected | cached
    final_action: str | None
    proposal: dict[str, Any] | None
    routing: dict[str, Any] | None = None
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    alarms: list[dict[str, Any]] = field(default_factory=list)
    notifications: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pipeline:
    def __init__(
        self,
        agent: AgentInterface,
        guardrails: GuardrailEngine | None = None,
        store: Store | None = None,
        git_repo: str | None = None,
        router: TeamRouter | None = None,
    ):
        self.agent = agent
        self.guardrails = guardrails or GuardrailEngine()
        self.store = store or Store()
        self.git_repo = git_repo or os.environ.get("GITHUB_REPO") or os.environ.get("DEMO_GIT_REPO", "")
        self.router = router or TeamRouter()

    def run(self, payload: dict[str, Any]) -> RunResult:
        try:
            result = self._run_inner(payload)
            log.info(
                "pipeline.end",
                extra={"status": result.status, "final_action": result.final_action},
            )
            return result
        finally:
            observability.set_run_id(None)

    def _run_inner(self, payload: dict[str, Any]) -> RunResult:
        material = parse_sentry_payload(payload)

        # Dedup
        existing = self.store.lookup_existing_run(material.event_id)
        if existing and existing.get("status") in {"ok", "escalated"}:
            observability.set_run_id(existing["run_id"])
            log.info("dedup hit for event_id=%s", material.event_id)
            cached = self.store.get_run(existing["run_id"])
            return RunResult(
                run_id=existing["run_id"],
                status="cached",
                final_action=existing.get("final_action"),
                proposal=None,
                checkpoints=[],
                alarms=cached.get("alarms", []),
            )

        run_id = self.store.start_run(material.event_id, self.agent.name)
        observability.set_run_id(run_id)
        log.info(
            "pipeline.start",
            extra={"event_id": material.event_id, "agent": self.agent.name},
        )
        self.store.record_stage(run_id, "material", payload, material.__dict__)

        alarms_emitted: list[dict[str, Any]] = []
        checkpoints_log: list[dict[str, Any]] = []

        def emit(name: str, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
            a = alarms_mod.emit(name, ctx).to_dict()
            alarms_emitted.append(a)
            self.store.record_alarm(run_id, a)
            return a

        # -------- Guardrails (pre) --------
        input_results = self.guardrails.check_input(material)
        self.store.record_stage(
            run_id,
            "guardrails_input",
            material.event_id,
            [r.__dict__ for r in input_results],
            passed=all(r.passed for r in input_results),
        )
        if not all(r.passed for r in input_results):
            failed = [r.rule for r in input_results if not r.passed]
            emit("INPUT_INVALID", {"failed_rules": failed})
            self.store.finish_run(run_id, "rejected", None)
            return RunResult(
                run_id=run_id,
                status="rejected",
                final_action=None,
                proposal=None,
                checkpoints=checkpoints_log,
                alarms=alarms_emitted,
            )

        rate_result = self.guardrails.check_rate(material.service)
        self.store.record_stage(run_id, "guardrails_rate", material.service, rate_result.__dict__, passed=rate_result.passed)
        if not rate_result.passed:
            emit("RATE_LIMIT_EXCEEDED", rate_result.details)

        budget_result = self.guardrails.check_budget(tokens_about_to_spend=self.guardrails.config["budget"]["per_incident_token_cap"])
        self.store.record_stage(run_id, "guardrails_budget", {}, budget_result.__dict__, passed=budget_result.passed)
        if not budget_result.passed:
            emit("BUDGET_EXCEEDED", budget_result.details)
            notif = self._escalate(material, None, alarms_emitted)
            self.store.record_stage(run_id, "notify_escalation", {}, notif)
            self.store.finish_run(run_id, "escalated", None)
            return RunResult(
                run_id=run_id,
                status="escalated",
                final_action="escalate_human",
                proposal=None,
                checkpoints=checkpoints_log,
                alarms=alarms_emitted,
                notifications=notif,
            )

        # -------- Runbook + git context --------
        runbook = tools.lookup_runbook(material.tags + [material.service])
        if runbook is None:
            emit("NO_RUNBOOK_MATCH", {"tags": material.tags, "service": material.service})
        self.store.record_stage(run_id, "runbook_lookup", material.tags, runbook or {})

        # -------- Team routing --------
        route = self.router.route(material, runbook)
        self.store.record_stage(run_id, "team_routing", {"service": material.service, "tags": material.tags}, route.to_dict())
        if route.unrouted:
            emit("ESCALATION_REQUIRED", {"reason": "no team matched", "fallback": route.to_dict()})

        git_ctx = tools.collect_git_context(self.git_repo, material.service, material.stack_files)
        self.store.record_stage(run_id, "git_context", {"repo": self.git_repo, "files": material.stack_files}, git_ctx)
        if not git_ctx.get("available"):
            emit("GIT_TIMEOUT", {"reason": git_ctx.get("reason", "")})

        # -------- Agent --------
        try:
            proposal = self.agent.execute(material, runbook, git_ctx)
        except Exception as e:
            log.exception("agent failed")
            emit("AGENT_OUTPUT_INVALID", {"error": str(e), "agent": self.agent.name})
            notif = self._escalate(material, None, alarms_emitted)
            self.store.record_stage(run_id, "notify_escalation", {}, notif)
            self.store.finish_run(run_id, "escalated", None)
            return RunResult(
                run_id=run_id,
                status="escalated",
                final_action="escalate_human",
                proposal=None,
                checkpoints=checkpoints_log,
                alarms=alarms_emitted,
                notifications=notif,
            )
        if proposal.tokens_used:
            self.guardrails.record_token_spend(proposal.tokens_used)
        self.store.record_stage(run_id, "agent", {"agent": self.agent.name}, proposal.to_dict())

        # -------- Checkpoints --------
        cps = []
        cp_signal = ck.signal_sufficiency(material)
        cp_runbook = ck.runbook_match(runbook)
        cp_git = ck.git_analysis(material, git_ctx)
        cps.append(cp_signal)
        cps.append(cp_runbook)
        cps.append(cp_git)

        # -------- Confidence cross-check --------
        # LLM agents self-report confidence; harness independently scores from
        # runbook/git/signal checkpoints and caps the LLM if signals are weak.
        # Heuristic agent IS the signal scorer — skip cross-check, but record
        # the fields so downstream consumers always see them.
        conf_cfg = self.guardrails.config.get("confidence") or {}
        floor = float(conf_cfg.get("signal_floor", conf_mod.DEFAULT_FLOOR))
        cap = float(conf_cfg.get("cap_when_low_signal", conf_mod.DEFAULT_CAP))
        weights = conf_cfg.get("components") or None

        llm_self_report = proposal.confidence
        if self.agent.name == "heuristic":
            proposal.llm_reported_confidence = llm_self_report
            proposal.harness_signal_score = llm_self_report
            proposal.confidence_components = None
            proposal.confidence_cap_applied = False
            proposal.confidence_reason = "heuristic agent: no cross-check"
            cross_stage = {
                "llm_reported": llm_self_report,
                "harness_signal": llm_self_report,
                "components": None,
                "cap_applied": False,
                "reason": proposal.confidence_reason,
                "final": proposal.confidence,
            }
        else:
            sig = conf_mod.compute_signal_score(cp_runbook, cp_git, cp_signal, weights)
            cross = conf_mod.apply_cross_check(llm_self_report, sig["score"], floor, cap)
            proposal.llm_reported_confidence = llm_self_report
            proposal.harness_signal_score = sig["score"]
            proposal.confidence_components = sig["components"]
            proposal.confidence_cap_applied = cross["applied_cap"]
            proposal.confidence_reason = cross["reason"]
            proposal.confidence = cross["final"]  # final value used by downstream gates
            cross_stage = {
                "llm_reported": llm_self_report,
                "harness_signal": sig["score"],
                "components": sig["components"],
                "cap_applied": cross["applied_cap"],
                "reason": cross["reason"],
                "final": cross["final"],
            }
        self.store.record_stage(run_id, "confidence_cross_check", {"agent": self.agent.name}, cross_stage)

        action_rules = self.guardrails.config["action_ladder"].get(proposal.recommended_action, {})
        min_conf = action_rules.get("min_confidence", 0.0)
        cps.append(ck.confidence_threshold(proposal, min_conf))

        action_guard = self.guardrails.check_action(
            proposal.recommended_action,
            material.severity,
            proposal.confidence,
            runbook_matched=(runbook is not None),
        )
        cps.append(ck.action_safety(proposal, action_guard.__dict__))

        for c in cps:
            checkpoints_log.append(c.to_dict())
            self.store.record_stage(run_id, f"checkpoint:{c.name}", {}, c.to_dict(), passed=c.passed)

        # -------- Decide --------
        # Critical checkpoints: signal_sufficiency + action_safety
        critical_failed = [c for c in cps if not c.passed and c.name in {"signal_sufficiency", "action_safety"}]
        soft_failed = [c for c in cps if not c.passed and c.name not in {"signal_sufficiency", "action_safety"}]

        if critical_failed:
            for c in critical_failed:
                if c.name == "action_safety":
                    emit("ACTION_OUT_OF_BOUNDS", {"checkpoint": c.to_dict(), "proposal": proposal.to_dict()})
                else:
                    emit("ESCALATION_REQUIRED", {"checkpoint": c.to_dict()})
            notif = self._escalate(material, proposal, alarms_emitted, route)
            self.store.record_stage(run_id, "notify_escalation", proposal.to_dict(), notif)
            self.store.finish_run(run_id, "escalated", "escalate_human")
            return RunResult(
                run_id=run_id,
                status="escalated",
                final_action="escalate_human",
                proposal=proposal.to_dict(),
                routing=route.to_dict(),
                checkpoints=checkpoints_log,
                alarms=alarms_emitted,
                notifications=notif,
            )

        # Soft checkpoint failures emit informational alarms but allow continuation
        for c in soft_failed:
            if c.name == "runbook_match":
                pass  # already emitted NO_RUNBOOK_MATCH
            elif c.name == "git_analysis":
                emit("GIT_ANALYSIS_INCONCLUSIVE", c.to_dict())
            elif c.name == "confidence_threshold":
                emit("LOW_CONFIDENCE", c.to_dict())

        # -------- Execute action --------
        notif = self._execute_action(material, proposal, alarms_emitted, route)
        self.store.record_stage(run_id, "notify", proposal.to_dict(), notif)
        self.store.finish_run(run_id, "ok", proposal.recommended_action)

        return RunResult(
            run_id=run_id,
            status="ok",
            final_action=proposal.recommended_action,
            proposal=proposal.to_dict(),
            routing=route.to_dict(),
            checkpoints=checkpoints_log,
            alarms=alarms_emitted,
            notifications=notif,
        )

    # -------- side effects --------

    def _execute_action(
        self,
        material: IncidentMaterial,
        proposal: Proposal,
        alarms_emitted: list[dict[str, Any]],
        route: TeamRoute,
    ) -> dict[str, Any]:
        mat_dict = {
            "event_id": material.event_id,
            "service": material.service,
            "severity": material.severity,
            "title": material.title,
        }
        route_dict = route.to_dict()
        slack_text = notifiers.format_slack_message(proposal.to_dict(), mat_dict, alarms_emitted, route_dict)
        notifications: dict[str, Any] = {
            "slack": notifiers.post_slack(slack_text, channel=route.slack),
            "routed_team": route_dict,
        }
        if proposal.recommended_action == "jira_create":
            notifications["jira"] = notifiers.create_jira(
                title=f"[{material.severity}] [{route.team_id}] {material.title}",
                body=proposal.hypothesis,
                fields={
                    "service": material.service,
                    "event_id": material.event_id,
                    "team": route.team_id,
                },
            )
        if proposal.recommended_action == "page_secondary":
            notifications["pagerduty"] = notifiers.page_pagerduty(
                summary=material.title,
                severity=material.severity,
                context={
                    "event_id": material.event_id,
                    "hypothesis": proposal.hypothesis,
                    "team": route.team_id,
                    "pagerduty_service": route.pagerduty_service,
                },
            )
        return notifications

    def _escalate(
        self,
        material: IncidentMaterial,
        proposal: Proposal | None,
        alarms_emitted: list[dict[str, Any]],
        route: TeamRoute | None = None,
    ) -> dict[str, Any]:
        mat_dict = {
            "event_id": material.event_id,
            "service": material.service,
            "severity": material.severity,
            "title": material.title,
        }
        proposal_dict = proposal.to_dict() if proposal else {"hypothesis": "(none — escalated before agent or invalid)", "recommended_action": "escalate_human", "confidence": 0.0}
        route_dict = route.to_dict() if route else None
        text = "🚨 ESCALATION\n" + notifiers.format_slack_message(proposal_dict, mat_dict, alarms_emitted, route_dict)
        channel = route.slack if route else None
        notif: dict[str, Any] = {
            "slack": notifiers.post_slack(text, channel=channel),
            "routed_team": route_dict,
        }
        notif["pagerduty"] = notifiers.page_pagerduty(
            summary=f"Triage harness escalation: {material.title}",
            severity=material.severity,
            context={
                "event_id": material.event_id,
                "alarms": [a["name"] for a in alarms_emitted],
                "team": (route.team_id if route else "unrouted"),
                "pagerduty_service": (route.pagerduty_service if route else "incident-manager"),
            },
        )
        return notif
