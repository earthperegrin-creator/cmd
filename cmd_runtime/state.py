"""Transactional CMD v2 job state and compatibility import."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .jobspec import CompiledJob


TERMINAL_STATES = frozenset({"completed", "partial", "blocked", "failed", "cancelled"})
ENGINES = frozenset({"v1", "v2_shadow", "v2"})
ACTIVE_RUN_STATES = frozenset({"claimed", "running", "committing", "verifying"})
TRANSITIONS = {
    "received": {"compiled", "failed", "cancelled"},
    "compiled": {"validated", "blocked", "failed", "cancelled"},
    "validated": {"queued", "awaiting_approval", "blocked", "failed", "cancelled"},
    "queued": {"running", "cancelled", "failed"},
    "running": {"awaiting_approval", "committing", "verifying", "partial", "blocked", "failed", "cancelled"},
    "awaiting_approval": {"queued", "committing", "cancelled", "failed"},
    "committing": {"verifying", "retrying", "failed", "cancelled"},
    "retrying": {"queued", "committing", "failed", "cancelled"},
    "verifying": {"completed", "partial", "blocked", "failed", "cancelled"},
}


class StateError(ValueError):
    """Raised when a state mutation violates the durable contract."""


@dataclass(frozen=True)
class ClaimedRun:
    run_id: str
    job_id: str
    revision_id: str
    spec_hash: str
    idempotency_key: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value


def provider_idempotency_key(spec_hash: str, operation_index: int) -> str:
    if operation_index < 0:
        raise StateError("operation_index must be non-negative")
    return hashlib.sha256(f"{spec_hash}:{operation_index}".encode("utf-8")).hexdigest()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, raw_request TEXT NOT NULL, ui_context_json TEXT NOT NULL,
  status TEXT NOT NULL, engine TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_revisions (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), revision_number INTEGER NOT NULL,
  spec_hash TEXT NOT NULL UNIQUE, spec_json TEXT NOT NULL, previous_spec_hash TEXT,
  created_at TEXT NOT NULL, UNIQUE(job_id, revision_number)
);
CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES jobs(id),
  revision_id TEXT REFERENCES job_revisions(id), event_type TEXT NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, payload_json TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), revision_id TEXT NOT NULL REFERENCES job_revisions(id),
  status TEXT NOT NULL, worker_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  claimed_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, ended_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_revision
  ON job_runs(revision_id) WHERE status IN ('claimed','running','committing','verifying');
CREATE TABLE IF NOT EXISTS capability_leases (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision_id TEXT NOT NULL, spec_hash TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE, grants_json TEXT NOT NULL, expires_at TEXT NOT NULL,
  call_budget INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT, lease_id TEXT NOT NULL, capability TEXT NOT NULL,
  payload_hash TEXT NOT NULL, status TEXT NOT NULL, code TEXT NOT NULL,
  provider_artifact_id TEXT, idempotency_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision_id TEXT NOT NULL, capability TEXT NOT NULL,
  payload_hash TEXT NOT NULL, status TEXT NOT NULL, approved_by TEXT, created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision_id TEXT NOT NULL, kind TEXT NOT NULL,
  locator TEXT NOT NULL, sha256 TEXT, provider_id TEXT, metadata_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_results (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, revision_id TEXT NOT NULL, criterion TEXT NOT NULL,
  status TEXT NOT NULL, evidence_json TEXT NOT NULL, verifier TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cleanup_records (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, artifact_id TEXT, capability TEXT NOT NULL,
  status TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS legacy_imports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_path TEXT NOT NULL, row_number INTEGER NOT NULL,
  row_hash TEXT NOT NULL, parse_status TEXT NOT NULL, row_json TEXT, error TEXT,
  imported_at TEXT NOT NULL, UNIQUE(source_path, row_number, row_hash)
);
CREATE TABLE IF NOT EXISTS runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


class JobStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO runtime_settings(key,value,updated_at) VALUES('engine','v1',?)",
                (_now(),),
            )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
        finally:
            connection.close()

    def create_job(self, compiled: CompiledJob, *, engine: str = "v2_shadow") -> str:
        if engine not in ENGINES:
            raise StateError(f"unsupported engine: {engine}")
        spec = compiled.spec
        revision_id = f"rev-{uuid.uuid4().hex}"
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                    (spec.job_id, spec.raw_instruction, json.dumps(_jsonable(compiled.ui_context), sort_keys=True),
                     "received", engine, timestamp, timestamp),
                )
                connection.execute(
                    "INSERT INTO job_revisions VALUES(?,?,?,?,?,?,?)",
                    (revision_id, spec.job_id, 1, spec.spec_hash,
                     json.dumps(spec.to_dict(), sort_keys=True), spec.previous_spec_hash, timestamp),
                )
                self._transition_tx(connection, spec.job_id, "compiled", "job_compiled", {}, f"{spec.job_id}:compiled", revision_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return revision_id

    def get_engine(self) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM runtime_settings WHERE key='engine'").fetchone()
        return str(row["value"]) if row and row["value"] in ENGINES else "v1"

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT j.*,r.id revision_id,r.spec_hash,r.spec_json FROM jobs j "
                "JOIN job_revisions r ON r.job_id=j.id AND r.revision_number=(SELECT MAX(r2.revision_number) FROM job_revisions r2 WHERE r2.job_id=j.id) "
                "ORDER BY j.created_at DESC,j.id DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [self._job_summary(row) for row in rows]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT j.*,r.id revision_id,r.spec_hash,r.spec_json FROM jobs j "
                "JOIN job_revisions r ON r.job_id=j.id WHERE j.id=? ORDER BY r.revision_number DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if not row:
                return None
            detail = self._job_summary(row)
            detail["events"] = self._rows(connection, "SELECT * FROM job_events WHERE job_id=? ORDER BY id", job_id)
            detail["runs"] = self._rows(connection, "SELECT * FROM job_runs WHERE job_id=? ORDER BY claimed_at", job_id)
            detail["leases"] = self._rows(connection, "SELECT * FROM capability_leases WHERE job_id=? ORDER BY created_at", job_id)
            detail["approvals"] = self._rows(connection, "SELECT * FROM approvals WHERE job_id=? ORDER BY created_at", job_id)
            detail["artifacts"] = self._rows(connection, "SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at", job_id)
            detail["verification"] = self._rows(connection, "SELECT * FROM verification_results WHERE job_id=? ORDER BY created_at", job_id)
            detail["cleanup"] = self._rows(connection, "SELECT * FROM cleanup_records WHERE job_id=? ORDER BY created_at", job_id)
            lease_ids = [row["id"] for row in detail["leases"]]
            detail["calls"] = []
            if lease_ids:
                placeholders = ",".join("?" for _ in lease_ids)
                detail["calls"] = [
                    self._decode_row(item)
                    for item in connection.execute(
                        f"SELECT * FROM capability_calls WHERE lease_id IN ({placeholders}) ORDER BY created_at", lease_ids,
                    ).fetchall()
                ]
        return detail

    def cancel_job(self, job_id: str, *, kill: bool = False) -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise StateError(f"unknown job: {job_id}")
            current = str(row["status"])
        if current in TERMINAL_STATES:
            return current
        state = self.transition(
            job_id,
            "cancelled",
            "kill_requested" if kill else "cancel_requested",
            payload={"hard_kill": kill},
            idempotency_key=f"{job_id}:{'kill' if kill else 'cancel'}",
        )
        with self._connect() as connection:
            connection.execute(
                "UPDATE job_runs SET status='cancelled',ended_at=? WHERE job_id=? AND status IN ('claimed','running','committing','verifying')",
                (_now(), job_id),
            )
        return state

    @staticmethod
    def _rows(connection: sqlite3.Connection, query: str, value: str) -> list[dict[str, Any]]:
        return [JobStateStore._decode_row(row) for row in connection.execute(query, (value,)).fetchall()]

    @staticmethod
    def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in tuple(value):
            if key.endswith("_json") and isinstance(value[key], str):
                try:
                    value[key[:-5]] = json.loads(value.pop(key))
                except json.JSONDecodeError:
                    pass
        return value

    @staticmethod
    def _job_summary(row: sqlite3.Row) -> dict[str, Any]:
        value = JobStateStore._decode_row(row)
        spec = value.pop("spec", {})
        ui_context = value.pop("ui_context", {})
        value["spec"] = spec
        value["ui_context"] = ui_context
        value["task_binding"] = spec.get("task_binding") if isinstance(spec, dict) else None
        value["context_sources"] = spec.get("context_refs", []) if isinstance(spec, dict) else []
        value["capabilities"] = spec.get("capability_grants", []) if isinstance(spec, dict) else []
        value["legacy_status"] = {
            "received": "pending", "compiled": "pending", "validated": "pending", "queued": "pending",
            "running": "running", "committing": "running", "verifying": "running", "retrying": "running",
            "awaiting_approval": "awaiting_approval", "completed": "completed", "partial": "failed",
            "blocked": "blocked", "failed": "failed", "cancelled": "cancelled",
        }.get(str(value.get("status")), "pending")
        return value

    def transition(
        self,
        job_id: str,
        to_state: str,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str,
    ) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT job_id,event_type,to_state FROM job_events WHERE idempotency_key=?", (idempotency_key,),
                ).fetchone()
                if existing:
                    if existing["job_id"] != job_id or existing["event_type"] != event_type:
                        raise StateError("idempotency key belongs to a different event")
                    connection.commit()
                    return str(existing["to_state"])
                revision = connection.execute(
                    "SELECT id FROM job_revisions WHERE job_id=? ORDER BY revision_number DESC LIMIT 1", (job_id,),
                ).fetchone()
                if not revision:
                    raise StateError(f"unknown job: {job_id}")
                self._transition_tx(connection, job_id, to_state, event_type, payload or {}, idempotency_key, revision["id"])
                connection.commit()
                return to_state
            except Exception:
                connection.rollback()
                raise

    def add_revision(self, compiled: CompiledJob) -> str:
        spec = compiled.spec
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                duplicate = connection.execute(
                    "SELECT id FROM job_revisions WHERE spec_hash=?", (spec.spec_hash,),
                ).fetchone()
                if duplicate:
                    connection.commit()
                    return str(duplicate["id"])
                latest = connection.execute(
                    "SELECT id,revision_number,spec_hash FROM job_revisions WHERE job_id=? ORDER BY revision_number DESC LIMIT 1",
                    (spec.job_id,),
                ).fetchone()
                if not latest:
                    raise StateError(f"unknown job: {spec.job_id}")
                if spec.previous_spec_hash != latest["spec_hash"]:
                    raise StateError("revision previous_spec_hash does not match the latest revision")
                active = connection.execute(
                    "SELECT 1 FROM job_runs WHERE job_id=? AND status IN ('claimed','running','committing','verifying') LIMIT 1",
                    (spec.job_id,),
                ).fetchone()
                if active:
                    raise StateError("cannot revise a job with an active run")
                revision_id = f"rev-{uuid.uuid4().hex}"
                timestamp = _now()
                connection.execute(
                    "INSERT INTO job_revisions VALUES(?,?,?,?,?,?,?)",
                    (revision_id, spec.job_id, int(latest["revision_number"]) + 1, spec.spec_hash,
                     json.dumps(spec.to_dict(), sort_keys=True), spec.previous_spec_hash, timestamp),
                )
                current = connection.execute("SELECT status FROM jobs WHERE id=?", (spec.job_id,)).fetchone()[0]
                connection.execute(
                    "INSERT INTO job_events(job_id,revision_id,event_type,from_state,to_state,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (spec.job_id, revision_id, "job_revised", current, "compiled", "{}",
                     f"{spec.job_id}:revision:{spec.spec_hash}", timestamp),
                )
                connection.execute("UPDATE jobs SET status='compiled',updated_at=? WHERE id=?", (timestamp, spec.job_id))
                connection.commit()
                return revision_id
            except Exception:
                connection.rollback()
                raise

    def _transition_tx(self, connection, job_id, to_state, event_type, payload, key, revision_id):
        row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise StateError(f"unknown job: {job_id}")
        current = str(row["status"])
        if current in TERMINAL_STATES or to_state not in TRANSITIONS.get(current, set()):
            raise StateError(f"illegal transition: {current} -> {to_state}")
        timestamp = _now()
        connection.execute(
            "INSERT INTO job_events(job_id,revision_id,event_type,from_state,to_state,payload_json,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (job_id, revision_id, event_type, current, to_state, json.dumps(payload, sort_keys=True), key, timestamp),
        )
        connection.execute("UPDATE jobs SET status=?,updated_at=? WHERE id=?", (to_state, timestamp, job_id))

    def claim_next(self, worker_id: str) -> ClaimedRun | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT j.id job_id,r.id revision_id,r.spec_hash FROM jobs j JOIN job_revisions r ON r.job_id=j.id "
                    "WHERE j.status='queued' AND r.revision_number=(SELECT MAX(r2.revision_number) FROM job_revisions r2 WHERE r2.job_id=j.id) "
                    "ORDER BY j.created_at,j.id LIMIT 1"
                ).fetchone()
                if not row:
                    connection.commit()
                    return None
                run_id = f"run-{uuid.uuid4().hex}"
                key = provider_idempotency_key(row["spec_hash"], 0)
                timestamp = _now()
                connection.execute(
                    "INSERT INTO job_runs VALUES(?,?,?,?,?,?,?,?,?)",
                    (run_id, row["job_id"], row["revision_id"], "running", worker_id, key, timestamp, timestamp, None),
                )
                self._transition_tx(connection, row["job_id"], "running", "run_claimed", {"run_id": run_id, "worker_id": worker_id}, f"claim:{run_id}", row["revision_id"])
                connection.commit()
                return ClaimedRun(run_id, row["job_id"], row["revision_id"], row["spec_hash"], key)
            except sqlite3.IntegrityError:
                connection.rollback()
                return None
            except Exception:
                connection.rollback()
                raise

    def record(self, table: str, values: Mapping[str, Any]) -> None:
        allowed = {
            "capability_leases", "capability_calls", "approvals", "artifacts",
            "verification_results", "cleanup_records",
        }
        if table not in allowed:
            raise StateError(f"unsupported record table: {table}")
        columns = tuple(values)
        if not columns:
            raise StateError("record values cannot be empty")
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*", column) for column in columns):
            raise StateError("record column name is invalid")
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )

    def finish_run(self, run_id: str, status: str) -> None:
        if status not in {"completed", "failed", "cancelled"}:
            raise StateError("invalid terminal run status")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE job_runs SET status=?,ended_at=? WHERE id=? AND status IN ('claimed','running','committing','verifying')",
                (status, _now(), run_id),
            )
            if cursor.rowcount != 1:
                raise StateError("run is not active")

    def import_legacy_jsonl(self, path: Path) -> dict[str, int]:
        content = path.read_bytes()
        counts = {"ok": 0, "malformed": 0}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for number, raw in enumerate(content.splitlines(), 1):
                    digest = hashlib.sha256(raw).hexdigest()
                    try:
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            raise ValueError("row is not an object")
                        status, row_json, error = "ok", json.dumps(value, sort_keys=True), None
                    except (ValueError, json.JSONDecodeError) as exc:
                        status, row_json, error = "malformed", None, str(exc)
                    connection.execute(
                        "INSERT OR IGNORE INTO legacy_imports(source_path,row_number,row_hash,parse_status,row_json,error,imported_at) VALUES(?,?,?,?,?,?,?)",
                        (str(path), number, digest, status, row_json, error, _now()),
                    )
                    counts[status] += 1
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        if path.read_bytes() != content:
            raise StateError("legacy import mutated its source")
        return counts

    def set_engine(self, engine: str) -> str:
        if engine not in ENGINES:
            raise StateError(f"unsupported engine: {engine}")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO runtime_settings(key,value,updated_at) VALUES('engine',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (engine, _now()),
            )
        return engine

    def engine(self) -> str:
        return self.get_engine()

    def job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise StateError(f"unknown job: {job_id}")
            return dict(row)

    def count(self, table: str) -> int:
        allowed = {"jobs", "job_revisions", "job_events", "job_runs", "legacy_imports"}
        if table not in allowed:
            raise StateError("unsupported count table")
        with self._connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
