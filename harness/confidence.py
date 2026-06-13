"""Harness-derived confidence cross-check.

LLM agents self-report confidence. The harness computes an independent signal
score from deterministic checkpoint outputs (runbook match, git analysis,
signal sufficiency). If the harness score is below a configured floor, the
LLM-reported confidence is capped — the demo story is: trust the LLM only when
independent signals back it up.

Both functions here are pure (no I/O).
"""
from __future__ import annotations

from typing import Any


DEFAULT_WEIGHTS = {
    "runbook_match": 0.4,
    "git_analysis": 0.4,
    "signal_sufficiency": 0.2,
}
DEFAULT_FLOOR = 0.6
DEFAULT_CAP = 0.4


def compute_signal_score(
    cp_runbook: Any,
    cp_git: Any,
    cp_signal: Any,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Weighted sum of three checkpoint scores.

    Each `cp_*` is a CheckpointResult-like object with a `.score` attribute in
    [0, 1]. Returns `{score, components}` where `components` is per-checkpoint
    raw scores (not weighted) for transparency in the Slack/dashboard view.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        # only override known keys; ignore unknown keys defensively
        for k in DEFAULT_WEIGHTS:
            if k in weights:
                w[k] = float(weights[k])

    total_w = w["runbook_match"] + w["git_analysis"] + w["signal_sufficiency"]
    if total_w <= 0:
        total_w = 1.0  # avoid divide-by-zero; degenerate config

    runbook = float(getattr(cp_runbook, "score", 0.0) or 0.0)
    git = float(getattr(cp_git, "score", 0.0) or 0.0)
    signal = float(getattr(cp_signal, "score", 0.0) or 0.0)

    weighted = (
        runbook * w["runbook_match"]
        + git * w["git_analysis"]
        + signal * w["signal_sufficiency"]
    ) / total_w

    return {
        "score": round(max(0.0, min(1.0, weighted)), 4),
        "components": {
            "runbook_match": round(runbook, 4),
            "git_analysis": round(git, 4),
            "signal_sufficiency": round(signal, 4),
        },
    }


def apply_cross_check(
    llm_conf: float,
    harness_score: float,
    floor: float = DEFAULT_FLOOR,
    cap: float = DEFAULT_CAP,
) -> dict[str, Any]:
    """Cap LLM confidence when harness signals are weak.

    Rule: if `harness_score < floor`, the final confidence is `min(llm_conf, cap)`.
    Otherwise the LLM self-report is trusted as-is.
    """
    llm_conf = float(llm_conf or 0.0)
    harness_score = float(harness_score or 0.0)
    if harness_score < floor:
        final = min(llm_conf, cap)
        applied = final < llm_conf
        reason = (
            f"harness signal {harness_score:.2f} < floor {floor:.2f}; "
            f"{'capped' if applied else 'no cap needed'} LLM {llm_conf:.2f} → {final:.2f}"
        )
        return {"final": round(final, 4), "applied_cap": applied, "reason": reason}
    return {
        "final": round(llm_conf, 4),
        "applied_cap": False,
        "reason": f"harness signal {harness_score:.2f} ≥ floor {floor:.2f}; trusting LLM {llm_conf:.2f}",
    }
