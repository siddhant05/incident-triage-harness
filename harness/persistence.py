"""SQLite persistence. Run log + per-stage checkpoint state. Replayable."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


DEFAULT_DB = Path(__file__).parent.parent / "harness.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    event_id TEXT,
    started_at REAL,
    completed_at REAL,
    status TEXT,
    agent_name TEXT,
    final_action TEXT
);
CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    stage_name TEXT,
    timestamp REAL,
    input_json TEXT,
    output_json TEXT,
    passed INTEGER
);
CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    timestamp REAL,
    name TEXT,
    severity TEXT,
    context_json TEXT,
    recommended_action TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_event ON runs(event_id);
CREATE INDEX IF NOT EXISTS idx_stages_run ON stages(run_id, stage_name);
"""


class Store:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = str(db_path)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def lookup_existing_run(self, event_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM runs WHERE event_id = ? ORDER BY started_at DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            return dict(row) if row else None

    def start_run(self, event_id: str, agent_name: str) -> str:
        run_id = uuid.uuid4().hex
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (run_id, event_id, started_at, status, agent_name) VALUES (?, ?, ?, ?, ?)",
                (run_id, event_id, time.time(), "running", agent_name),
            )
        return run_id

    def finish_run(self, run_id: str, status: str, final_action: str | None) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE runs SET completed_at = ?, status = ?, final_action = ? WHERE run_id = ?",
                (time.time(), status, final_action, run_id),
            )

    def record_stage(
        self,
        run_id: str,
        stage_name: str,
        input_obj: Any,
        output_obj: Any,
        passed: bool | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO stages (run_id, stage_name, timestamp, input_json, output_json, passed) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    stage_name,
                    time.time(),
                    json.dumps(input_obj, default=str),
                    json.dumps(output_obj, default=str),
                    None if passed is None else int(passed),
                ),
            )

    def record_alarm(self, run_id: str, alarm: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO alarms (run_id, timestamp, name, severity, context_json, recommended_action) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    time.time(),
                    alarm["name"],
                    alarm["severity"],
                    json.dumps(alarm.get("context", {}), default=str),
                    alarm.get("recommended_action", ""),
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._conn() as c:
            run = c.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            stages = c.execute(
                "SELECT * FROM stages WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
            alarms = c.execute(
                "SELECT * FROM alarms WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
        return {
            "run": dict(run) if run else None,
            "stages": [dict(s) for s in stages],
            "alarms": [dict(a) for a in alarms],
        }

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_stage_output(self, run_id: str, stage_name: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT output_json FROM stages WHERE run_id = ? AND stage_name = ? ORDER BY id DESC LIMIT 1",
                (run_id, stage_name),
            ).fetchone()
            if not row:
                return None
            return json.loads(row["output_json"])
