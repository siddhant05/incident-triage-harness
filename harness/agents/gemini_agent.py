"""Gemini-backed triage agent. Implements AgentInterface."""
from __future__ import annotations

import json
import os
from typing import Any

from ..material import IncidentMaterial
from .base import Proposal


SYSTEM_PROMPT = """You are an incident triage agent inside a governed harness.

You receive: a normalized incident, an optional matching runbook, and git context (recent commits + blame for files in the stack trace).

You MUST respond with a JSON object only, no prose, no markdown fences:
{
  "hypothesis": "<short root-cause hypothesis>",
  "recommended_action": "<one of: slack_post | jira_create | page_secondary | suggest_rollback>",
  "confidence": <float 0..1>,
  "routing_team": "<team slug or null>",
  "suspect_files": ["path/to/file.py", ...],
  "suspect_commits": [{"sha": "...", "author": "...", "msg": "..."}, ...]
}

Rules:
- Be conservative with confidence. 0.9+ only if a recent commit clearly touches a file in the stack trace.
- If no runbook and no git signal: confidence <= 0.4, recommended_action = "slack_post".
- Never recommend mark_resolved.
"""


class GeminiAgent:
    name = "gemini"

    def __init__(self, model: str = "gemini-2.5-flash"):
        try:
            import google.generativeai as genai  # type: ignore
        except ImportError as e:
            raise RuntimeError("google-generativeai package not installed") from e
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY env var required")
        genai.configure(api_key=api_key)
        self._genai = genai
        self._model_name = model
        self._model = genai.GenerativeModel(
            model_name=model,
            system_instruction=SYSTEM_PROMPT,
            generation_config={"response_mime_type": "application/json", "temperature": 0.2},
        )

    def execute(
        self,
        material: IncidentMaterial,
        runbook: dict[str, Any] | None,
        git_context: dict[str, Any],
    ) -> Proposal:
        user_msg = json.dumps(
            {
                "incident": {
                    "event_id": material.event_id,
                    "service": material.service,
                    "severity": material.severity,
                    "title": material.title,
                    "message": material.message,
                    "tags": material.tags,
                    "stack_files": material.stack_files,
                },
                "runbook": runbook,
                "git_context": git_context,
            },
            indent=2,
        )
        resp = self._model.generate_content(user_msg)
        text = (resp.text or "").strip()
        data = _parse_json(text)
        usage = getattr(resp, "usage_metadata", None)
        tokens = (getattr(usage, "prompt_token_count", 0) + getattr(usage, "candidates_token_count", 0)) if usage else 0
        return Proposal(
            hypothesis=data.get("hypothesis", ""),
            recommended_action=data.get("recommended_action", "slack_post"),
            confidence=float(data.get("confidence", 0.0)),
            routing_team=data.get("routing_team"),
            suspect_files=list(data.get("suspect_files") or []),
            suspect_commits=list(data.get("suspect_commits") or []),
            runbook_id=(runbook or {}).get("id"),
            raw_agent_output=data,
            tokens_used=tokens,
        )


def _parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            return json.loads(text[s : e + 1])
        raise
