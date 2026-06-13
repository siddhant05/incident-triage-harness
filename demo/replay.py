#!/usr/bin/env python3
"""CLI replay: feed a payload file through the pipeline. Usage:
    python -m demo.replay demo/sample_payload.json [--agent heuristic|claude]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Make project importable when run as script
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.agents.heuristic_agent import HeuristicAgent
from harness.pipeline import Pipeline
from harness.persistence import Store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("payload", help="Path to JSON payload")
    ap.add_argument("--agent", default="heuristic", choices=["heuristic", "claude", "gemini"])
    ap.add_argument(
        "--repo",
        default=None,
        help="Git repo spec: GitHub `owner/repo` or local path (overrides GITHUB_REPO/DEMO_GIT_REPO)",
    )
    args = ap.parse_args()

    payload = json.loads(Path(args.payload).read_text())

    if args.agent == "claude":
        from harness.agents.claude_agent import ClaudeAgent
        agent = ClaudeAgent()
    elif args.agent == "gemini":
        from harness.agents.gemini_agent import GeminiAgent
        agent = GeminiAgent()
    else:
        agent = HeuristicAgent()

    repo_spec = args.repo or os.environ.get("GITHUB_REPO") or os.environ.get("DEMO_GIT_REPO")
    pipeline = Pipeline(agent=agent, store=Store(), git_repo=repo_spec)
    result = pipeline.run(payload)
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
