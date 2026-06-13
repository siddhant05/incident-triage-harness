"""Structured logging + metrics for the Incident Triage Harness.

- JSON line logging via JsonFormatter
- run_id correlation via contextvars + RunIdFilter
- metrics() helper that aggregates SQLite state for /metrics endpoint
"""
from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from .persistence import Store


# --- run_id correlation ---------------------------------------------------

_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_run_id", default=None
)


def set_run_id(run_id: str | None) -> None:
    _run_id_var.set(run_id)


def get_run_id() -> str | None:
    return _run_id_var.get()


class RunIdFilter(logging.Filter):
    """Attach the current ContextVar run_id to every log record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D401
        if not hasattr(record, "run_id") or getattr(record, "run_id", None) is None:
            rid = _run_id_var.get()
            if rid is not None:
                record.run_id = rid
        return True


# --- JSON formatter -------------------------------------------------------

# stdlib LogRecord standard attributes we don't want duplicated in the JSON.
_STD_LOGRECORD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    """One JSON object per log line. Robust to missing run_id and weird extras."""

    def format(self, record: logging.LogRecord) -> str:
        # Render message lazily (handles %-formatting safely).
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)

        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }

        rid = getattr(record, "run_id", None)
        if rid is not None:
            payload["run_id"] = rid

        # Pull any non-standard attributes into the payload (these are `extra=`).
        for k, v in record.__dict__.items():
            if k in _STD_LOGRECORD_ATTRS or k == "run_id":
                continue
            if k.startswith("_"):
                continue
            try:
                json.dumps(v, default=str)
                payload[k] = v
            except Exception:
                payload[k] = repr(v)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        try:
            return json.dumps(payload, default=str)
        except Exception:
            # Last-resort: never crash logging.
            return json.dumps({
                "ts": payload["ts"],
                "level": payload["level"],
                "logger": payload["logger"],
                "msg": msg,
            })


# --- setup helper ---------------------------------------------------------

def configure_logging(json: bool = True, level: str = "INFO") -> None:
    """Configure the root logger. Idempotent (clears existing handlers)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    if json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
    handler.addFilter(RunIdFilter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


# --- metrics --------------------------------------------------------------

_SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
_STATUSES = ("ok", "escalated", "rejected", "cached")


def metrics(store: Store) -> dict[str, Any]:
    """Aggregate counts from SQLite. Returns dict shape documented in spec."""
    out: dict[str, Any] = {
        "runs_total": 0,
        "runs_by_status": {s: 0 for s in _STATUSES},
        "runs_by_agent": {},
        "alarms_by_name": {},
        "alarms_by_severity": {s: 0 for s in _SEVERITIES},
        "last_run_ts": 0.0,
    }
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row

        total = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        out["runs_total"] = int(total or 0)

        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM runs GROUP BY status"
        ):
            status = row["status"] or "unknown"
            out["runs_by_status"][status] = (
                out["runs_by_status"].get(status, 0) + int(row["n"])
            )

        for row in conn.execute(
            "SELECT agent_name, COUNT(*) AS n FROM runs GROUP BY agent_name"
        ):
            name = row["agent_name"] or "unknown"
            out["runs_by_agent"][name] = int(row["n"])

        for row in conn.execute(
            "SELECT name, COUNT(*) AS n FROM alarms GROUP BY name"
        ):
            out["alarms_by_name"][row["name"] or "unknown"] = int(row["n"])

        for row in conn.execute(
            "SELECT severity, COUNT(*) AS n FROM alarms GROUP BY severity"
        ):
            sev = (row["severity"] or "").upper() or "UNKNOWN"
            out["alarms_by_severity"][sev] = (
                out["alarms_by_severity"].get(sev, 0) + int(row["n"])
            )

        last = conn.execute(
            "SELECT MAX(COALESCE(completed_at, started_at)) AS ts FROM runs"
        ).fetchone()
        out["last_run_ts"] = float(last["ts"] or 0.0)

    return out
