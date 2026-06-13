"""Pytest wrapper: run the eval harness programmatically with the heuristic
agent (deterministic, no API key required) and assert all cases pass.
"""
from __future__ import annotations

from pathlib import Path

import pytest


CASES_DIR = Path(__file__).resolve().parents[1] / "evals" / "cases"
EXPECTED_CASES = {
    "users_api_null_pointer",
    "checkout_db_timeout",
    "ads_tracker_error",
    "unknown_service_unrouted",
    "malformed_payload",
}


def test_eval_fixtures_present():
    if not CASES_DIR.is_dir():
        pytest.skip(f"evals cases dir not found: {CASES_DIR}")
    found = {p.stem for p in CASES_DIR.glob("*.json")}
    missing = EXPECTED_CASES - found
    if missing:
        pytest.skip(f"missing eval fixtures: {sorted(missing)}")


def test_run_eval_heuristic_all_pass():
    if not CASES_DIR.is_dir():
        pytest.skip(f"evals cases dir not found: {CASES_DIR}")
    found = {p.stem for p in CASES_DIR.glob("*.json")}
    if EXPECTED_CASES - found:
        pytest.skip(f"missing eval fixtures: {sorted(EXPECTED_CASES - found)}")

    from evals.run_eval import run_eval

    passed, total, outcomes = run_eval(
        agent_name="heuristic", cases_dir=CASES_DIR, verbose=False
    )
    if passed != total:
        # Build a readable failure report.
        lines = [f"{passed}/{total} eval cases passed"]
        for o in outcomes:
            if not o.passed:
                lines.append(f"  FAIL {o.case.slug}: {o.case.name}")
                for f in o.failures:
                    lines.append(f"    - {f}")
        pytest.fail("\n".join(lines))
