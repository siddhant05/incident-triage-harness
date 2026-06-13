"""Material handler. Normalize raw Sentry payload into IncidentMaterial."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


REDACT_PATTERNS = {
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}


@dataclass
class StackFrame:
    filename: str
    function: str
    lineno: int | None = None


@dataclass
class IncidentMaterial:
    event_id: str
    service: str
    severity: str  # P0..P4
    title: str
    message: str
    tags: list[str] = field(default_factory=list)
    stack_frames: list[StackFrame] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def stack_files(self) -> list[str]:
        return [f.filename for f in self.stack_frames if f.filename]


def _redact(text: str) -> str:
    for pattern in REDACT_PATTERNS.values():
        text = pattern.sub("[REDACTED]", text)
    return text


def parse_sentry_payload(payload: dict[str, Any]) -> IncidentMaterial:
    """Normalize Sentry webhook payload. Tolerant of missing fields."""
    event = payload.get("event") or payload.get("data", {}).get("event") or payload
    event_id = event.get("event_id") or payload.get("id") or "unknown"
    tags_raw = event.get("tags") or []
    # Sentry tags arrive as [["key", "value"], ...]
    tags = []
    service = "unknown"
    severity = "P3"
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, list) and len(t) == 2:
                k, v = t
                tags.append(f"{k}:{v}")
                if k == "service":
                    service = v
                if k == "level":
                    severity = _level_to_severity(v)
                if k == "severity":
                    severity = v
    title = _redact(str(event.get("title") or event.get("message") or "untitled"))
    message = _redact(str(event.get("message") or event.get("logentry", {}).get("message") or ""))
    frames = _extract_frames(event)
    return IncidentMaterial(
        event_id=str(event_id),
        service=service,
        severity=severity,
        title=title,
        message=message,
        tags=tags,
        stack_frames=frames,
        raw=payload,
    )


def _level_to_severity(level: str) -> str:
    mapping = {"fatal": "P0", "error": "P2", "warning": "P3", "info": "P4", "debug": "P4"}
    return mapping.get(level.lower(), "P3")


def _extract_frames(event: dict[str, Any]) -> list[StackFrame]:
    frames: list[StackFrame] = []
    exception = event.get("exception") or {}
    values = exception.get("values") if isinstance(exception, dict) else None
    if not values:
        return frames
    for val in values:
        stacktrace = val.get("stacktrace") or {}
        for f in stacktrace.get("frames") or []:
            filename = f.get("filename") or f.get("abs_path") or ""
            frames.append(
                StackFrame(
                    filename=filename,
                    function=f.get("function") or "",
                    lineno=f.get("lineno"),
                )
            )
    return frames
