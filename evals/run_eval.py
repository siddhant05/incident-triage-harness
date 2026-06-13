"""Eval harness for the Incident Triage Pipeline.

Loads fixture JSON files from `evals/cases/` and runs each through a fresh
Pipeline + Store, asserting `expected.*` fields against the RunResult.

Usage:
    python -m evals.run_eval --agent heuristic
    python -m evals.run_eval --agent gemini --case users_api_null_pointer
    python -m evals.run_eval --cases-dir evals/cases
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.persistence import Store
from harness.pipeline import Pipeline, RunResult


DEFAULT_CASES_DIR = Path(__file__).parent / "cases"


# -------- agent factory --------

def _build_agent(name: str):
    name = name.lower()
    if name == "heuristic":
        from harness.agents.heuristic_agent import HeuristicAgent
        return HeuristicAgent()
    if name == "gemini":
        from harness.agents.gemini_agent import GeminiAgent  # type: ignore
        return GeminiAgent()
    if name == "claude":
        from harness.agents.claude_agent import ClaudeAgent  # type: ignore
        return ClaudeAgent()
    raise ValueError(f"unknown agent: {name}")


# -------- case loading --------

@dataclass
class Case:
    file: Path
    name: str
    payload: dict[str, Any]
    expected: dict[str, Any]

    @property
    def slug(self) -> str:
        return self.file.stem


def load_cases(cases_dir: Path, only: str | None = None) -> list[Case]:
    if not cases_dir.is_dir():
        raise FileNotFoundError(f"cases dir not found: {cases_dir}")
    cases: list[Case] = []
    for path in sorted(cases_dir.glob("*.json")):
        data = json.loads(path.read_text())
        cases.append(
            Case(
                file=path,
                name=data.get("name", path.stem),
                payload=data["payload"],
                expected=data.get("expected", {}),
            )
        )
    if only:
        cases = [c for c in cases if c.slug == only or c.name == only]
        if not cases:
            raise SystemExit(f"no case matched: {only}")
    return cases


# -------- assertion --------

@dataclass
class CaseOutcome:
    case: Case
    passed: bool
    failures: list[str] = field(default_factory=list)
    result: RunResult | None = None
    error: str | None = None


def _assert_case(case: Case, result: RunResult) -> list[str]:
    """Return list of human-readable failure reasons. Empty list = pass."""
    expected = case.expected
    failures: list[str] = []

    if "status" in expected:
        if result.status != expected["status"]:
            failures.append(
                f"status: expected {expected['status']!r}, got {result.status!r}"
            )

    if "status_in" in expected:
        allowed = expected["status_in"]
        if result.status not in allowed:
            failures.append(f"status: expected one of {allowed}, got {result.status!r}")

    if "routed_team" in expected:
        team = (result.routing or {}).get("team_id")
        if team != expected["routed_team"]:
            failures.append(
                f"routed_team: expected {expected['routed_team']!r}, got {team!r}"
            )

    if "min_confidence" in expected:
        conf = (result.proposal or {}).get("confidence")
        if conf is None:
            failures.append(
                f"min_confidence: expected >= {expected['min_confidence']}, got no proposal"
            )
        elif conf < expected["min_confidence"]:
            failures.append(
                f"min_confidence: expected >= {expected['min_confidence']}, got {conf}"
            )

    if "suspect_file_contains_any" in expected:
        needles = expected["suspect_file_contains_any"]
        suspect = (result.proposal or {}).get("suspect_files") or []
        ok = any(any(n in s for s in suspect) for n in needles)
        if not ok:
            failures.append(
                f"suspect_file_contains_any: expected any of {needles}, "
                f"got suspect_files={suspect}"
            )

    if "final_action_in" in expected:
        allowed = expected["final_action_in"]
        if result.final_action not in allowed:
            failures.append(
                f"final_action: expected one of {allowed}, got {result.final_action!r}"
            )

    if "alarm_name_in" in expected:
        required = set(expected["alarm_name_in"])
        seen = {a.get("name") for a in (result.alarms or [])}
        if not (required & seen):
            failures.append(
                f"alarm_name_in: expected at least one of {sorted(required)}, "
                f"got {sorted(n for n in seen if n)}"
            )

    return failures


# -------- run --------

def run_case(case: Case, agent_name: str, db_path: Path) -> CaseOutcome:
    try:
        agent = _build_agent(agent_name)
        store = Store(db_path)
        # Pass empty git_repo so evals don't depend on network or local clone.
        pipeline = Pipeline(agent=agent, store=store, git_repo="")
        result = pipeline.run(case.payload)
    except Exception as e:  # surface as test failure, don't blow up the run
        return CaseOutcome(case=case, passed=False, failures=[f"exception: {e!r}"], error=str(e))

    failures = _assert_case(case, result)
    return CaseOutcome(case=case, passed=not failures, failures=failures, result=result)


def run_eval(
    agent_name: str = "heuristic",
    cases_dir: Path | None = None,
    only: str | None = None,
    verbose: bool = True,
) -> tuple[int, int, list[CaseOutcome]]:
    """Run all eval cases. Returns (passed, total, outcomes)."""
    cases = load_cases(cases_dir or DEFAULT_CASES_DIR, only=only)
    outcomes: list[CaseOutcome] = []
    with tempfile.TemporaryDirectory(prefix="eval_") as td:
        td_path = Path(td)
        for i, case in enumerate(cases):
            db_path = td_path / f"eval_{i}_{case.slug}.db"
            outcome = run_case(case, agent_name, db_path)
            outcomes.append(outcome)
            if verbose:
                _print_outcome(outcome)
    passed = sum(1 for o in outcomes if o.passed)
    if verbose:
        print(f"\nSummary: {passed}/{len(outcomes)} passed (agent={agent_name})")
    return passed, len(outcomes), outcomes


def _print_outcome(o: CaseOutcome) -> None:
    tag = "PASS" if o.passed else "FAIL"
    print(f"[{tag}] {o.case.slug}: {o.case.name}")
    if not o.passed:
        for f in o.failures:
            print(f"    - {f}")
        if o.result is not None:
            r = o.result
            print(
                f"    (got status={r.status} final_action={r.final_action} "
                f"team={(r.routing or {}).get('team_id')} "
                f"confidence={(r.proposal or {}).get('confidence')})"
            )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run incident-triage eval cases.")
    ap.add_argument(
        "--agent",
        choices=["heuristic", "gemini", "claude"],
        default="heuristic",
        help="Which agent to run (default: heuristic).",
    )
    ap.add_argument("--case", default=None, help="Run only one case by slug or name.")
    ap.add_argument(
        "--cases-dir",
        default=str(DEFAULT_CASES_DIR),
        help=f"Directory of case JSON fixtures (default: {DEFAULT_CASES_DIR}).",
    )
    args = ap.parse_args(argv)

    passed, total, _ = run_eval(
        agent_name=args.agent,
        cases_dir=Path(args.cases_dir),
        only=args.case,
        verbose=True,
    )
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
