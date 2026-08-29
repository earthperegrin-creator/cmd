#!/usr/bin/env python3
"""SQLite operational state for Command.

SQLite is the canonical operational record: stable task records, lifecycle
state, queue actions, receipts, artifacts, report events, and priority signals.
Markdown remains the human-readable priority/journal layer.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cmd_runtime.google_doc_contract import (
    google_doc_contract_summary,
    result_blocks_on_google_doc_contract,
)


SCHEMA_VERSION = 9
REPORTABLE_CATEGORIES = {"deal", "post", "comms", "auto", "admin"}
PICKUP_MISS_RECOVERY_SECONDS = 86400


def configured_user_email() -> str:
    return os.environ.get("CMD_USER_EMAIL", "").strip().lower()


def gmail_message_url(thread_id: str, message_id: str = "") -> str:
    target = thread_id or message_id
    if not target:
        return ""
    email = configured_user_email()
    account = f"?authuser={email}" if email else "0/"
    return f"https://mail.google.com/mail/u/{account}#all/{target}"
SUBSTANTIVE_REPORT_TERMS = {
    "call",
    "meeting",
    "met ",
    "founder",
    "diligence",
    "intro",
    "introduced",
    "team",
    "terms",
    "round",
    "valuation",
    "arr",
    "revenue",
    "traction",
    "memo",
    "decision",
    "pass",
    "passed",
    "extend",
    "extension",
    "portfolio",
    "investor",
    "customer",
    "enterprise",
    "screening",
    "sourcing",
}
MECHANICAL_REPORT_TERMS = {
    "draft saved",
    "saved gmail draft",
    "marked done",
    "checked off",
    "no external side effects",
    "no email was sent",
    "task can now close",
}


class CommandConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class WorkItemHierarchyError(ValueError):
    """A requested outcome/subtask relationship violates hierarchy rules."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=CommandConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True)


def normalize_email_priority(value: str) -> str:
    lowered = (value or "").strip().lower()
    if lowered in {"red", "high", "urgent"}:
        return "high"
    if lowered in {"yellow", "medium", "med"}:
        return "medium"
    return "low"


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def reportable_category(category: str) -> bool:
    return category in REPORTABLE_CATEGORIES


def report_candidate_fields(category: str, title: str, body: str, latest_receipt_summary: str | None = None) -> dict[str, Any]:
    """Classify a completed task for weekly-report surfacing.

    Receipts are noisy by design, so this does not make them reportable events.
    It only helps the report writer avoid missing completed deal/post/admin work
    that never became a polished weekly-log bullet.
    """
    if not reportable_category(category):
        return {
            "reportable": False,
            "report_bucket": "excluded",
            "report_strength": "excluded",
            "report_reason": f"category '{category}' is excluded from firm weekly reports",
        }

    text = " ".join(part for part in [title, body, latest_receipt_summary or ""] if part).lower()
    has_substance = any(term in text for term in SUBSTANTIVE_REPORT_TERMS)
    mechanical_hits = sum(1 for term in MECHANICAL_REPORT_TERMS if term in text)

    if has_substance:
        return {
            "reportable": True,
            "report_bucket": "core",
            "report_strength": "high" if category in {"deal", "post"} else "medium",
            "report_reason": "completed reportable item contains substantive business context",
        }

    if category == "deal":
        return {
            "reportable": True,
            "report_bucket": "pipeline_followup",
            "report_strength": "low",
            "report_reason": "completed deal item has light context; surface in pipeline/follow-up review instead of dropping",
        }

    if mechanical_hits:
        return {
            "reportable": True,
            "report_bucket": "admin_review",
            "report_strength": "low",
            "report_reason": "completed reportable item appears mostly mechanical; review before including",
        }

    return {
        "reportable": True,
        "report_bucket": "review",
        "report_strength": "medium",
        "report_reason": "completed reportable item needs human review for weekly-report wording",
    }


def week_id_from_path(path: Path) -> str:
    match = re.search(r"(20\d{2})-W(\d{2})", path.name)
    return f"{match.group(1)}-W{match.group(2)}" if match else ""


def week_start_at(path: Path) -> str:
    match = re.search(r"(20\d{2})-W(\d{2})", path.name)
    if not match:
        return utc_now()
    day = date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    return f"{day.isoformat()}T00:00:00+00:00"


def init_db(path: Path) -> None:
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                uri TEXT NOT NULL,
                label TEXT NOT NULL,
                checksum TEXT,
                observed_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(source_type, uri)
            );

            CREATE TABLE IF NOT EXISTS work_items (
                item_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                urgency TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                source_state TEXT NOT NULL DEFAULT 'present',
                current_source_id TEXT REFERENCES sources(source_id),
                source_fingerprint TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT,
                closed_at TEXT,
                done_at TEXT,
                last_action_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                parent_item_id TEXT REFERENCES work_items(item_id),
                sort_order INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS work_item_sources (
                item_id TEXT NOT NULL REFERENCES work_items(item_id) ON DELETE CASCADE,
                source_id TEXT NOT NULL REFERENCES sources(source_id) ON DELETE CASCADE,
                source_item_id TEXT,
                line_start INTEGER,
                line_end INTEGER,
                raw_text TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(item_id, source_id, source_item_id)
            );

            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                item_id TEXT REFERENCES work_items(item_id),
                kind TEXT NOT NULL,
                origin TEXT,
                instruction TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                risk_level TEXT,
                risk_label TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS receipts (
                receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL,
                status TEXT NOT NULL,
                effective_status TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                model TEXT,
                created_at TEXT NOT NULL,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                item_id TEXT REFERENCES work_items(item_id),
                action_id TEXT,
                actor_type TEXT NOT NULL,
                turn_type TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                dispatch_id TEXT,
                run_type TEXT NOT NULL DEFAULT 'agent',
                status TEXT NOT NULL DEFAULT 'queued',
                started_at TEXT,
                finished_at TEXT,
                model TEXT,
                error TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS run_actions (
                run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                action_id TEXT NOT NULL,
                PRIMARY KEY(run_id, action_id)
            );

            CREATE TABLE IF NOT EXISTS dispatches (
                dispatch_id TEXT PRIMARY KEY,
                mode TEXT,
                status TEXT,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS dispatch_actions (
                dispatch_id TEXT NOT NULL REFERENCES dispatches(dispatch_id) ON DELETE CASCADE,
                action_id TEXT NOT NULL,
                PRIMARY KEY(dispatch_id, action_id)
            );

            CREATE TABLE IF NOT EXISTS approvals (
                approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL,
                approved INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                action_id TEXT,
                item_id TEXT REFERENCES work_items(item_id),
                artifact_type TEXT NOT NULL,
                label TEXT NOT NULL,
                uri TEXT,
                external_id TEXT,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS priority_signals (
                signal_id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT REFERENCES work_items(item_id),
                signal_type TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                observed_at TEXT NOT NULL,
                source_action_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS work_item_events (
                event_id TEXT PRIMARY KEY,
                item_id TEXT REFERENCES work_items(item_id),
                event_type TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,
                week_id TEXT NOT NULL DEFAULT '',
                reportable INTEGER NOT NULL DEFAULT 0,
                source_type TEXT NOT NULL DEFAULT '',
                source_id TEXT,
                source_item_id TEXT,
                source_action_id TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS email_candidates (
                candidate_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'gmail',
                message_id TEXT NOT NULL,
                thread_id TEXT,
                sender TEXT NOT NULL DEFAULT '',
                recipients TEXT NOT NULL DEFAULT '',
                subject TEXT NOT NULL DEFAULT '',
                snippet TEXT NOT NULL DEFAULT '',
                received_at TEXT NOT NULL DEFAULT '',
                labels TEXT NOT NULL DEFAULT '',
                proposed_title TEXT NOT NULL DEFAULT '',
                proposed_body TEXT NOT NULL DEFAULT '',
                proposed_category TEXT NOT NULL DEFAULT '',
                proposed_urgency TEXT NOT NULL DEFAULT 'low',
                reason TEXT NOT NULL DEFAULT '',
                score REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'proposed',
                item_id TEXT REFERENCES work_items(item_id),
                observed_at TEXT NOT NULL,
                decided_at TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(provider, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
            CREATE INDEX IF NOT EXISTS idx_work_items_category ON work_items(category);
            CREATE INDEX IF NOT EXISTS idx_actions_item ON actions(item_id);
            CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
            CREATE INDEX IF NOT EXISTS idx_receipts_action ON receipts(action_id);
            CREATE INDEX IF NOT EXISTS idx_turns_item ON turns(item_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_turns_action ON turns(action_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_runs_dispatch ON runs(dispatch_id);
            CREATE INDEX IF NOT EXISTS idx_run_actions_action ON run_actions(action_id);
            CREATE INDEX IF NOT EXISTS idx_priority_item ON priority_signals(item_id);
            CREATE INDEX IF NOT EXISTS idx_events_occurred ON work_item_events(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_events_week ON work_item_events(week_id);
            CREATE INDEX IF NOT EXISTS idx_events_reportable ON work_item_events(reportable, category, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_email_candidates_status ON email_candidates(status, observed_at);
            CREATE INDEX IF NOT EXISTS idx_email_candidates_message ON email_candidates(provider, message_id);
            """
        )
        ensure_column(conn, "work_items", "closed_at", "TEXT")
        ensure_column(conn, "work_items", "done_at", "TEXT")
        ensure_column(conn, "work_items", "last_action_at", "TEXT")
        ensure_column(conn, "work_items", "today", "INTEGER NOT NULL DEFAULT 0")
        ensure_column(conn, "work_items", "parent_item_id", "TEXT")
        ensure_column(conn, "work_items", "sort_order", "INTEGER NOT NULL DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_done_at ON work_items(done_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_work_items_closed_at ON work_items(closed_at)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_items_parent_sort "
            "ON work_items(parent_item_id, sort_order, item_id)"
        )
        ensure_column(conn, "email_candidates", "thread_id", "TEXT")
        ensure_column(conn, "email_candidates", "decision_reason", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "email_candidates", "training_label", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "email_candidates", "training_rationale", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "email_candidates", "training_category", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "email_candidates", "training_priority", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "email_candidates", "training_relationship", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "email_candidates", "training_decided_at", "TEXT")
        ensure_column(conn, "email_candidates", "training_payload_json", "TEXT NOT NULL DEFAULT '{}'")
        ensure_column(conn, "artifacts", "turn_id", "TEXT")
        ensure_column(conn, "artifacts", "content", "TEXT NOT NULL DEFAULT ''")
        ensure_column(conn, "artifacts", "version", "INTEGER NOT NULL DEFAULT 1")
        ensure_column(conn, "artifacts", "supersedes_artifact_id", "TEXT")
        ensure_column(conn, "artifacts", "updated_at", "TEXT")
        ensure_column(conn, "actions", "reviewed_at", "TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_item ON artifacts(item_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_action ON artifacts(action_id, created_at)")
        version_row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        previous_version = int(version_row["value"]) if version_row and str(version_row["value"]).isdigit() else 0
        if previous_version < 9:
            # Existing terminal receipts predate the durable handoff inbox. Treat
            # them as already seen so the migration does not flood the queue.
            conn.execute(
                """
                UPDATE actions
                SET reviewed_at=COALESCE(updated_at, created_at)
                WHERE status IN ('completed', 'cancelled', 'dismissed')
                  AND reviewed_at IS NULL
                """
            )
        if previous_version < 6:
            conn.execute("UPDATE work_items SET source_state='present'")
            conn.execute(
                """
                UPDATE work_items
                SET current_source_id=NULL,
                    source_fingerprint=NULL
                WHERE current_source_id IN (
                  SELECT source_id FROM sources WHERE source_type='weekly_log'
                )
                """
            )
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            WHERE meta.value<>excluded.value
            """,
            ("schema_version", str(SCHEMA_VERSION)),
        )


def source_id(source_type: str, uri: str) -> str:
    return f"{source_type}:{uri}"


def upsert_source(conn: sqlite3.Connection, source_type: str, uri: str, label: str, metadata: dict[str, Any] | None = None) -> str:
    sid = source_id(source_type, uri)
    now = utc_now()
    conn.execute(
        """
        INSERT INTO sources(source_id, source_type, uri, label, observed_at, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
          label=excluded.label,
          observed_at=excluded.observed_at,
          metadata_json=excluded.metadata_json
        """,
        (sid, source_type, uri, label, now, dumps(metadata)),
    )
    return sid


def effective_result_status(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    summary = str(result.get("summary") or "").lower()
    if result_blocks_on_google_doc_contract(result):
        return "blocked"
    if (
        status in {"failed", "blocked"}
        and result.get("model") == "queue-watcher"
        and "no background worker picked up this action" in summary
    ):
        try:
            receipt_time = datetime.fromisoformat(str(result.get("time") or result.get("timestamp") or ""))
            if receipt_time.tzinfo is None:
                receipt_time = receipt_time.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - receipt_time).total_seconds() <= PICKUP_MISS_RECOVERY_SECONDS:
                return "pending"
        except ValueError:
            pass
    if status in {"failed", "blocked"} and "gmail draft" in summary:
        return "completed"
    return status


def receipt_marks_reviewed(result: dict[str, Any]) -> bool:
    """Return whether this receipt records a human acknowledgement, not agent work."""
    status = str(result.get("status") or "").lower()
    summary = str(result.get("summary") or "").lower()
    if status in {"dismissed", "cancelled"}:
        return True
    return any(marker in summary for marker in (
        "agent queue item handled by ",
        "handled by the user",
        "cleared this agent queue item",
        "dismissed from the active command agent queue",
        "exact preview approved; execution queued",
    ))


def infer_result_item_id(
    conn: sqlite3.Connection,
    result: dict[str, Any],
    *,
    require_active: bool = False,
) -> str | None:
    """Recover a durable outcome link returned by a free-form worker action."""
    candidates = [result.get("item_id"), result.get("task_id"), result.get("work_item_id")]
    artifact = result.get("artifact")
    text_parts = [result.get("summary"), result.get("conclusion"), result.get("content"), result.get("draft")]
    if isinstance(artifact, dict):
        candidates.extend((artifact.get("item_id"), artifact.get("task_id"), artifact.get("work_item_id")))
        text_parts.extend((artifact.get("content"), artifact.get("body"), artifact.get("text")))
    else:
        text_parts.append(artifact)
    for value in text_parts:
        text = str(value or "")
        for pattern in (
            r"(?:Task|Outcome|Work item)\s+ID\s*:\s*([A-Za-z0-9._:-]+)",
            r"CMD\s+outcome\s*:\s*[^\n]*?\(([A-Za-z0-9._:-]+)\)",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                candidates.append(match.group(1))
    for candidate in candidates:
        item_id = str(candidate or "").strip()
        if not item_id:
            continue
        row = conn.execute(
            "SELECT status FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row and (not require_active or str(row["status"] or "") in {"open", "awaiting_human", "blocked"}):
            return item_id
    return None


def infer_exact_active_capture_item_id(
    conn: sqlite3.Connection,
    action: dict[str, Any],
) -> str | None:
    """Recover only an exact, active canonical-capture lineage for old actions.

    This is deliberately narrower than semantic task matching. It repairs an
    action that repeated the exact development previously captured through the
    canonical resolver, while chatter and merely similar requests stay unbound.
    """
    if action.get("kind") != "agent_chat" or action.get("origin") != "agent_inbox":
        return None
    instruction = re.sub(r"\s+", " ", str(action.get("instruction") or "")).strip().casefold()
    if not instruction:
        return None
    rows = conn.execute(
        """
        SELECT DISTINCT w.item_id, wis.raw_text
        FROM work_items w
        JOIN work_item_sources wis ON wis.item_id=w.item_id
        JOIN sources s ON s.source_id=wis.source_id
        WHERE w.status IN ('open', 'awaiting_human', 'blocked')
          AND s.source_type='cmd_capture'
        """
    ).fetchall()
    matches = {
        str(row["item_id"])
        for row in rows
        if re.sub(r"\s+", " ", str(row["raw_text"] or "")).strip().casefold() == instruction
    }
    return next(iter(matches)) if len(matches) == 1 else None


def result_needs_normal_browser(summary: str) -> bool:
    lowered = (summary or "").lower()
    markers = [
        "in-app browser backend is unavailable",
        "browser is not available: iab",
        "available browser backend list was empty",
    ]
    return any(marker in lowered for marker in markers)


def result_requires_human_review(summary: str, explicit: bool = False) -> bool:
    if explicit:
        return True
    lowered = (summary or "").lower()
    markers = [
        "gmail draft",
        "saved draft",
        "draft ready",
        "draft is ready",
        "draft for review",
        "review and send",
    ]
    return any(marker in lowered for marker in markers)


def infer_item_status(
    current: str,
    action_kind: str,
    effective_status: str,
    summary: str = "",
    *,
    needs_human_review: bool = False,
) -> str:
    if effective_status == "cancelled":
        return current
    if effective_status == "dismissed":
        return "open" if current == "awaiting_human" else current
    if effective_status in {"blocked", "failed"}:
        return current if current not in {"done", "dropped", "superseded"} else current
    if effective_status != "completed":
        return current
    lowered = summary.lower()
    review_complete_markers = [
        "agent queue item handled by ",
        "handled by the user",
        "marked reviewed",
        "cleared this agent queue item",
    ]
    if current == "awaiting_human" and any(marker in lowered for marker in review_complete_markers):
        return "open"
    done_markers = [
        "manually completed",
        "task can now close",
        "marked done",
        "checking off",
        "checked off",
        "reconciled the command done",
        "reconciled the command done action",
    ]
    drop_markers = [
        "reconciled the command drop",
        "removed the w",
        "removing the w",
        "removed the stale",
        "superseded",
    ]
    if any(marker in lowered for marker in done_markers):
        return "done"
    if any(marker in lowered for marker in drop_markers):
        return "dropped"
    if action_kind == "done":
        return "done"
    if action_kind == "drop":
        return "dropped"
    if action_kind == "recover":
        return "open"
    if current not in {"done", "dropped", "cancelled", "superseded"} and result_requires_human_review(
        summary,
        explicit=needs_human_review,
    ):
        return "awaiting_human"
    return current


def upsert_work_item_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    item_id: str | None,
    event_type: str,
    category: str,
    subject: str,
    title: str,
    summary: str,
    status: str,
    occurred_at: str,
    week_id: str,
    source_type: str,
    source_id: str | None = None,
    source_item_id: str | None = None,
    source_action_id: str | None = None,
    reportable: bool | None = None,
    raw: dict[str, Any] | None = None,
) -> None:
    is_reportable = reportable_category(category) if reportable is None else reportable
    conn.execute(
        """
        INSERT INTO work_item_events(
          event_id, item_id, event_type, category, subject, title, summary, status,
          occurred_at, week_id, reportable, source_type, source_id, source_item_id,
          source_action_id, raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
          item_id=excluded.item_id,
          event_type=excluded.event_type,
          category=excluded.category,
          subject=excluded.subject,
          title=excluded.title,
          summary=excluded.summary,
          status=excluded.status,
          occurred_at=excluded.occurred_at,
          week_id=excluded.week_id,
          reportable=excluded.reportable,
          source_type=excluded.source_type,
          source_id=excluded.source_id,
          source_item_id=excluded.source_item_id,
          source_action_id=excluded.source_action_id,
          raw_json=excluded.raw_json
        """,
        (
            event_id,
            item_id,
            event_type,
            category,
            subject,
            title,
            summary,
            status,
            occurred_at,
            week_id,
            1 if is_reportable else 0,
            source_type,
            source_id,
            source_item_id,
            source_action_id,
            dumps(raw),
        ),
    )


def merge_metadata(existing: str | None, updates: dict[str, Any]) -> str:
    try:
        metadata = json.loads(existing or "{}")
    except json.JSONDecodeError:
        metadata = {}
    metadata.update(updates)
    return dumps(metadata)


def direct_overrides(metadata: dict[str, Any]) -> set[str]:
    values = metadata.get("direct_overrides")
    if isinstance(values, list):
        return {str(value) for value in values}
    return set()


def is_later_iso(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return left > right


def read_metadata(value: str | None) -> dict[str, Any]:
    try:
        metadata = json.loads(value or "{}")
    except json.JSONDecodeError:
        metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def get_work_item(path: Path, item_id: str) -> dict[str, Any] | None:
    return next((item for item in list_work_items(path) if item["id"] == item_id), None)


def candidate_id(provider: str, message_id: str) -> str:
    return f"{provider}:{message_id}"


def slugify_item_id(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return text or "task"


def normalized_task_identity(title: str) -> str:
    """Normalize harmless title edits without attempting semantic matching."""
    tokens = slugify_item_id(title).split("-")
    return "-".join(token for token in tokens if token not in {"a", "an", "the"})


def equivalent_active_item_ids(
    conn: sqlite3.Connection,
    title: str,
    category: str = "",
    parent_item_id: str | None = None,
) -> list[str]:
    identity = normalized_task_identity(title)
    rows = conn.execute(
        """
        SELECT item_id, title, category, parent_item_id
        FROM work_items
        WHERE status IN ('open', 'awaiting_human', 'blocked')
        """
    ).fetchall()
    return [
        str(row["item_id"])
        for row in rows
        if normalized_task_identity(str(row["title"])) == identity
        and (not category or not row["category"] or row["category"] == category)
        and row["parent_item_id"] == parent_item_id
    ]


def find_equivalent_active_work_item(
    path: Path,
    title: str,
    category: str = "",
    parent_item_id: str | None = None,
) -> dict[str, Any] | None:
    init_db(path)
    with connect(path) as conn:
        matches = equivalent_active_item_ids(conn, title, category, parent_item_id)
    if len(matches) != 1:
        return None
    return get_work_item(path, matches[0])


def email_context_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "thread_id",
            "sender_intent",
            "context",
            "suggested_action",
            "gmail_url",
            "triage_model",
            "triage_confidence",
            "relationship",
            "existing_item_id",
            "existing_item_title",
            "suggested_training_label",
            "training_group_key",
            "training_group_count",
        )
        if candidate.get(key) not in (None, "")
    }


def active_work_item_for_email_thread(
    path: Path,
    provider: str,
    thread_id: str,
    message_id: str = "",
) -> str | None:
    """Return the open work item already tracking this Gmail thread, if any."""
    if not thread_id:
        return None
    init_db(path)
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT c.item_id
            FROM email_candidates c
            JOIN work_items w ON w.item_id = c.item_id
            WHERE c.provider=?
              AND c.thread_id=?
              AND c.status='accepted'
              AND c.item_id IS NOT NULL
              AND w.status IN ('open', 'awaiting_human', 'blocked')
              AND (? = '' OR c.message_id != ?)
            ORDER BY c.observed_at DESC, c.decided_at DESC
            LIMIT 1
            """,
            (provider, thread_id, message_id, message_id),
        ).fetchone()
    return str(row["item_id"]) if row else None


def email_attention_summary(candidate: dict[str, Any]) -> str:
    sender = str(candidate.get("sender") or "").strip()
    sender_name = re.sub(r"<[^>]+>", "", sender).strip().strip('"') or "Gmail"
    action = str(candidate.get("suggested_action") or candidate.get("sender_intent") or candidate.get("snippet") or "").strip()
    if action:
        return f"New Gmail reply from {sender_name}: {action[:240]}"
    subject = str(candidate.get("subject") or "thread").strip()
    return f"New Gmail reply from {sender_name} on {subject[:180]}"


def mark_email_attention(path: Path, item_id: str, candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Attach a new inbound-reply attention marker to an existing work item."""
    init_db(path)
    now = utc_now()
    provider = str(candidate.get("provider") or "gmail")
    message_id = str(candidate.get("message_id") or candidate.get("id") or "")
    thread_id = str(candidate.get("thread_id") or "")
    if not item_id or not message_id:
        return get_work_item(path, item_id)
    gmail_url = str(candidate.get("gmail_url") or "")
    if not gmail_url and provider == "gmail":
        gmail_url = gmail_message_url(thread_id, message_id)
    summary = email_attention_summary(candidate)
    with connect(path) as conn:
        row = conn.execute(
            "SELECT status, metadata_json FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        metadata = read_metadata(row["metadata_json"])
        metadata.update(email_context_metadata({**candidate, "gmail_url": gmail_url}))
        metadata.update({
            "attention_kind": "email_reply",
            "attention_label": "New reply",
            "attention_summary": summary,
            "attention_at": now,
            "attention_source": "gmail",
            "attention_message_id": message_id,
            "attention_thread_id": thread_id,
            "attention_sender": str(candidate.get("sender") or ""),
            "attention_subject": str(candidate.get("subject") or ""),
            "attention_url": gmail_url,
            "last_email_reply_at": now,
            "last_email_reply_message_id": message_id,
        })
        sid = upsert_source(
            conn,
            provider,
            f"{provider}:{message_id}",
            str(candidate.get("subject") or message_id),
            {
                "message_id": message_id,
                "thread_id": thread_id,
                "candidate_id": candidate.get("candidate_id"),
                "attention_kind": "email_reply",
            },
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO work_item_sources(
              item_id, source_id, source_item_id, first_seen_at, last_seen_at, raw_text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, sid, message_id, now, now, str(candidate.get("snippet") or candidate.get("subject") or "")),
        )
        conn.execute(
            """
            UPDATE work_item_sources
            SET last_seen_at=?, raw_text=COALESCE(NULLIF(?, ''), raw_text)
            WHERE item_id=? AND source_id=? AND source_item_id=?
            """,
            (now, str(candidate.get("snippet") or ""), item_id, sid, message_id),
        )
        reopen_existing = bool(candidate.get("reopen_existing"))
        allowed_statuses = ("open", "awaiting_human", "blocked", "done", "dropped") if reopen_existing else ("open", "awaiting_human", "blocked")
        placeholders = ",".join("?" for _ in allowed_statuses)
        conn.execute(
            """
            UPDATE work_items
            SET status='awaiting_human',
                done_at=NULL,
                closed_at=NULL,
                updated_at=?,
                last_seen_at=?,
                current_source_id=?,
                source_fingerprint=?,
                metadata_json=?
            WHERE item_id=?
              AND status IN (""" + placeholders + """)
            """,
            (now, now, sid, f"{provider}:{message_id}", dumps(metadata), item_id, *allowed_statuses),
        )
        upsert_work_item_event(
            conn,
            event_id=f"email_attention:{item_id}:{message_id}",
            item_id=item_id,
            event_type="email_reply_attention",
            category="",
            subject=str(candidate.get("sender") or candidate.get("subject") or ""),
            title=str(candidate.get("subject") or "New Gmail reply"),
            summary=summary,
            status="awaiting_human",
            occurred_at=now,
            week_id="",
            source_type=provider,
            source_id=sid,
            source_item_id=message_id,
            reportable=False,
            raw={"candidate": candidate},
        )
    return get_work_item(path, item_id)


def clear_work_item_attention(path: Path, item_id: str) -> dict[str, Any] | None:
    init_db(path)
    now = utc_now()
    with connect(path) as conn:
        row = conn.execute(
            "SELECT status, category, title, metadata_json FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        metadata = read_metadata(row["metadata_json"])
        attention_kind = str(metadata.get("attention_kind") or "")
        for key in list(metadata):
            if key.startswith("attention_"):
                metadata.pop(key, None)
        next_status = "open" if row["status"] == "awaiting_human" and attention_kind == "email_reply" else row["status"]
        conn.execute(
            """
            UPDATE work_items
            SET status=?, updated_at=?, metadata_json=?
            WHERE item_id=?
            """,
            (next_status, now, dumps(metadata), item_id),
        )
        upsert_work_item_event(
            conn,
            event_id=f"email_attention_cleared:{item_id}:{now}",
            item_id=item_id,
            event_type="email_attention_cleared",
            category=str(row["category"] or ""),
            subject=str(row["title"] or ""),
            title=str(row["title"] or ""),
            summary="Cleared the Gmail reply attention marker; parent task remains open.",
            status=next_status,
            occurred_at=now,
            week_id="",
            source_type="cmd",
            source_id=None,
            source_item_id=None,
            reportable=False,
            raw={},
        )
    return get_work_item(path, item_id)


def upsert_email_candidate(path: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    init_db(path)
    provider = str(candidate.get("provider") or "gmail")
    message_id = str(candidate.get("message_id") or candidate.get("id") or "")
    if not message_id:
        raise ValueError("email candidate is missing message_id")
    cid = str(candidate.get("candidate_id") or candidate_id(provider, message_id))
    now = utc_now()
    revive_decided = bool(candidate.get("revive_decided"))
    with connect(path) as conn:
        existing = conn.execute(
            "SELECT status, item_id, decided_at FROM email_candidates WHERE candidate_id=?",
            (cid,),
        ).fetchone()
        preserve_decided = bool(existing and existing["status"] in {"accepted", "rejected", "done"} and not revive_decided)
        status = existing["status"] if preserve_decided else str(candidate.get("status") or "proposed")
        item_id = existing["item_id"] if preserve_decided and existing["item_id"] else candidate.get("item_id")
        decided_at = existing["decided_at"] if preserve_decided else candidate.get("decided_at")
        conn.execute(
            """
            INSERT INTO email_candidates(
              candidate_id, provider, message_id, thread_id, sender, recipients, subject,
              snippet, received_at, labels, proposed_title, proposed_body,
              proposed_category, proposed_urgency, reason, score, status, item_id,
              observed_at, decided_at, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id) DO UPDATE SET
              thread_id=excluded.thread_id,
              sender=excluded.sender,
              recipients=excluded.recipients,
              subject=excluded.subject,
              snippet=excluded.snippet,
              received_at=excluded.received_at,
              labels=excluded.labels,
              proposed_title=excluded.proposed_title,
              proposed_body=excluded.proposed_body,
              proposed_category=excluded.proposed_category,
              proposed_urgency=excluded.proposed_urgency,
              reason=excluded.reason,
              score=excluded.score,
              status=CASE
                WHEN ? = 0 AND email_candidates.status IN ('accepted', 'rejected', 'done') THEN email_candidates.status
                ELSE excluded.status
              END,
              item_id=CASE
                WHEN ? = 1 THEN excluded.item_id
                ELSE COALESCE(email_candidates.item_id, excluded.item_id)
              END,
              observed_at=excluded.observed_at,
              decided_at=CASE
                WHEN ? = 1 THEN excluded.decided_at
                ELSE COALESCE(email_candidates.decided_at, excluded.decided_at)
              END,
              decision_reason=CASE
                WHEN ? = 1 THEN ''
                ELSE email_candidates.decision_reason
              END,
              raw_json=excluded.raw_json
            """,
            (
                cid,
                provider,
                message_id,
                str(candidate.get("thread_id") or ""),
                str(candidate.get("sender") or ""),
                str(candidate.get("recipients") or ""),
                str(candidate.get("subject") or ""),
                str(candidate.get("snippet") or ""),
                str(candidate.get("received_at") or ""),
                str(candidate.get("labels") or ""),
                str(candidate.get("proposed_title") or ""),
                str(candidate.get("proposed_body") or ""),
                str(candidate.get("proposed_category") or "personal"),
                str(candidate.get("proposed_urgency") or "low"),
                str(candidate.get("reason") or ""),
                float(candidate.get("score") or 0),
                status,
                item_id,
                now,
                decided_at,
                dumps(candidate),
                int(revive_decided),
                int(revive_decided),
                int(revive_decided),
                int(revive_decided),
            ),
        )
        if existing and existing["status"] == "accepted" and existing["item_id"]:
            work_item = conn.execute(
                "SELECT metadata_json FROM work_items WHERE item_id=?",
                (existing["item_id"],),
            ).fetchone()
            if work_item:
                metadata = read_metadata(work_item["metadata_json"])
                overrides = direct_overrides(metadata)
                metadata.update(email_context_metadata(candidate))
                metadata["semantic_triage_refreshed_at"] = now
                updates = ["metadata_json=?", "updated_at=?", "last_seen_at=?"]
                params: list[Any] = [dumps(metadata), now, now]
                for field, column, fallback in (
                    ("title", "title", str(candidate.get("proposed_title") or "")),
                    ("body", "body", str(candidate.get("proposed_body") or "")),
                    ("category", "category", str(candidate.get("proposed_category") or "")),
                    ("urgency", "urgency", normalize_email_priority(str(candidate.get("proposed_urgency") or "low"))),
                ):
                    if field not in overrides and fallback:
                        updates.append(f"{column}=?")
                        params.append(fallback)
                params.append(existing["item_id"])
                conn.execute(
                    f"UPDATE work_items SET {', '.join(updates)} WHERE item_id=?",
                    params,
                )
    return get_email_candidate(path, cid) or {}


def get_email_candidate(path: Path, candidate_id_value: str) -> dict[str, Any] | None:
    rows = list_email_candidates(path, include_decided=True)
    return next((row for row in rows if row["candidate_id"] == candidate_id_value), None)


def list_email_candidates(path: Path, include_decided: bool = False) -> list[dict[str, Any]]:
    init_db(path)
    where = "" if include_decided else "WHERE status='proposed'"
    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              candidate_id, provider, message_id, thread_id, sender, recipients, subject,
              snippet, received_at, labels, proposed_title, proposed_body,
              proposed_category, proposed_urgency, reason, score, status, item_id,
              observed_at, decided_at, decision_reason, training_label, training_rationale,
              training_category, training_priority, training_relationship, training_decided_at,
              training_payload_json, raw_json
            FROM email_candidates
            {where}
            ORDER BY
              CASE status WHEN 'proposed' THEN 0 WHEN 'accepted' THEN 1 WHEN 'done' THEN 2 WHEN 'rejected' THEN 3 ELSE 4 END,
              score DESC,
              received_at DESC,
              observed_at DESC
            LIMIT 200
            """
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        raw = read_metadata(row["raw_json"])
        candidate = {
            "candidate_id": row["candidate_id"],
            "provider": row["provider"],
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "sender": row["sender"],
            "recipients": row["recipients"],
            "subject": row["subject"],
            "snippet": row["snippet"],
            "received_at": row["received_at"],
            "labels": row["labels"],
            "proposed_title": row["proposed_title"],
            "proposed_body": row["proposed_body"],
            "proposed_category": row["proposed_category"],
            "proposed_urgency": normalize_email_priority(row["proposed_urgency"]),
            "reason": row["reason"],
            "score": row["score"],
            "status": row["status"],
            "item_id": row["item_id"],
            "observed_at": row["observed_at"],
            "decided_at": row["decided_at"],
            "decision_reason": row["decision_reason"],
            "training_label": row["training_label"],
            "training_rationale": row["training_rationale"],
            "training_category": row["training_category"],
            "training_priority": row["training_priority"],
            "training_relationship": row["training_relationship"],
            "training_decided_at": row["training_decided_at"],
            "training_payload": read_metadata(row["training_payload_json"]),
        }
        candidate.update(email_context_metadata(raw))
        candidates.append(candidate)
    return candidates


def list_email_training_review(path: Path) -> list[dict[str, Any]]:
    """Return rows that should be visible in Gmail Training Review only."""

    init_db(path)
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              candidate_id, provider, message_id, thread_id, sender, recipients, subject,
              snippet, received_at, labels, proposed_title, proposed_body,
              proposed_category, proposed_urgency, reason, score, status, item_id,
              observed_at, decided_at, decision_reason, training_label, training_rationale,
              training_category, training_priority, training_relationship, training_decided_at,
              training_payload_json, raw_json
            FROM email_candidates
            WHERE status IN ('proposed', 'training_review')
            ORDER BY
              received_at DESC,
              observed_at DESC,
              CASE status WHEN 'proposed' THEN 0 WHEN 'training_review' THEN 1 ELSE 2 END,
              score DESC
            LIMIT 200
            """
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        raw = read_metadata(row["raw_json"])
        candidate = {
            "candidate_id": row["candidate_id"],
            "provider": row["provider"],
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "sender": row["sender"],
            "recipients": row["recipients"],
            "subject": row["subject"],
            "snippet": row["snippet"],
            "received_at": row["received_at"],
            "labels": row["labels"],
            "proposed_title": row["proposed_title"],
            "proposed_body": row["proposed_body"],
            "proposed_category": row["proposed_category"],
            "proposed_urgency": normalize_email_priority(row["proposed_urgency"]),
            "reason": row["reason"],
            "score": row["score"],
            "status": row["status"],
            "item_id": row["item_id"],
            "observed_at": row["observed_at"],
            "decided_at": row["decided_at"],
            "decision_reason": row["decision_reason"],
            "training_label": row["training_label"],
            "training_rationale": row["training_rationale"],
            "training_category": row["training_category"],
            "training_priority": row["training_priority"],
            "training_relationship": row["training_relationship"],
            "training_decided_at": row["training_decided_at"],
            "training_payload": read_metadata(row["training_payload_json"]),
        }
        candidate.update(email_context_metadata(raw))
        candidates.append(candidate)
    return candidates


def update_email_training_fields(
    conn: sqlite3.Connection,
    candidate_id_value: str,
    *,
    label: str,
    rationale: str = "",
    category: str = "",
    priority: str = "",
    relationship: str = "",
    payload: dict[str, Any] | None = None,
    decision_reason: str = "",
    decided_at: str | None = None,
) -> None:
    now = decided_at or utc_now()
    conn.execute(
        """
        UPDATE email_candidates
        SET training_label=?,
            training_rationale=?,
            training_category=?,
            training_priority=?,
            training_relationship=?,
            training_decided_at=?,
            training_payload_json=?,
            decision_reason=?
        WHERE candidate_id=?
        """,
        (
            label,
            rationale,
            category,
            priority,
            relationship,
            now,
            dumps(payload or {}),
            decision_reason or rationale,
            candidate_id_value,
        ),
    )


def email_candidate_statuses(path: Path, provider: str = "gmail") -> dict[str, str]:
    init_db(path)
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT message_id, status
            FROM email_candidates
            WHERE provider=?
            """,
            (provider,),
        ).fetchall()
    return {row["message_id"]: row["status"] for row in rows}


def retire_unseen_email_candidates(path: Path, provider: str, seen_message_ids: set[str]) -> int:
    init_db(path)
    with connect(path) as conn:
        if seen_message_ids:
            placeholders = ",".join("?" for _ in seen_message_ids)
            params = [provider, *sorted(seen_message_ids)]
            cursor = conn.execute(
                f"""
                UPDATE email_candidates
                SET status='superseded', decision_reason='not_in_latest_scan', decided_at=?
                WHERE provider=?
                  AND status='proposed'
                  AND message_id NOT IN ({placeholders})
                """,
                [utc_now(), *params],
            )
        else:
            cursor = conn.execute(
                """
                UPDATE email_candidates
                SET status='superseded', decision_reason='not_in_latest_scan', decided_at=?
                WHERE provider=? AND status='proposed'
                """,
                (utc_now(), provider),
            )
        return cursor.rowcount


def retire_filtered_email_candidates(path: Path, provider: str, filtered_message_ids: set[str]) -> int:
    if not filtered_message_ids:
        return 0
    init_db(path)
    with connect(path) as conn:
        placeholders = ",".join("?" for _ in filtered_message_ids)
        cursor = conn.execute(
            f"""
            UPDATE email_candidates
            SET status='superseded', decision_reason='filtered_by_classifier', decided_at=?
            WHERE provider=?
              AND status='proposed'
              AND message_id IN ({placeholders})
            """,
            [utc_now(), provider, *sorted(filtered_message_ids)],
        )
        return cursor.rowcount


def accept_email_candidate(path: Path, candidate_id_value: str, overrides: dict[str, Any] | None = None) -> dict[str, Any] | None:
    init_db(path)
    now = utc_now()
    overrides = overrides or {}
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM email_candidates WHERE candidate_id=?",
            (candidate_id_value,),
        ).fetchone()
        if not row:
            return None
        title = str(overrides.get("proposed_title") or row["proposed_title"] or row["subject"] or row["message_id"])
        body = str(overrides.get("proposed_body") or row["proposed_body"] or row["snippet"] or "")
        category = str(overrides.get("proposed_category") or row["proposed_category"] or "personal")
        urgency = normalize_email_priority(str(overrides.get("proposed_urgency") or row["proposed_urgency"] or "low"))
        raw_candidate = read_metadata(row["raw_json"])
        base_id = slugify_item_id(title)
        item_id = base_id
        suffix = 2
        while conn.execute("SELECT 1 FROM work_items WHERE item_id=?", (item_id,)).fetchone():
            item_id = f"{base_id}-{suffix}"
            suffix += 1
        source_uri = f"gmail:{row['message_id']}"
        sid = upsert_source(
            conn,
            "gmail",
            source_uri,
            row["subject"] or row["message_id"],
            {
                "message_id": row["message_id"],
                "thread_id": row["thread_id"],
                "candidate_id": candidate_id_value,
                **email_context_metadata(raw_candidate),
            },
        )
        metadata = {
            "created_from": "email_candidate",
            "candidate_id": candidate_id_value,
            "provider": row["provider"],
            "message_id": row["message_id"],
            "sender": row["sender"],
            "subject": row["subject"],
            "labels": row["labels"],
            "received_at": row["received_at"],
            "thread_id": row["thread_id"],
            **email_context_metadata(raw_candidate),
        }
        conn.execute(
            """
            INSERT INTO work_items(
              item_id, title, body, category, urgency, today, status, source_state,
              current_source_id, source_fingerprint, created_at, updated_at,
              last_seen_at, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, 0, 'open', 'present', ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                title,
                body,
                category,
                urgency,
                sid,
                source_uri,
                now,
                now,
                now,
                dumps(metadata),
            ),
        )
        conn.execute(
            """
            INSERT INTO work_item_sources(
              item_id, source_id, source_item_id, first_seen_at, last_seen_at, raw_text
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (item_id, sid, row["message_id"], now, now, row["snippet"] or row["subject"] or ""),
        )
        conn.execute(
            """
            UPDATE email_candidates
            SET status='accepted', item_id=?, decided_at=?
            WHERE candidate_id=?
            """,
            (item_id, now, candidate_id_value),
        )
        upsert_work_item_event(
            conn,
            event_id=f"email_candidate:{candidate_id_value}:accepted",
            item_id=item_id,
            event_type="email_candidate_accepted",
            category=category,
            subject=row["sender"] or row["subject"] or "",
            title=title,
            summary=f"Accepted Gmail candidate from {row['sender']}: {row['subject']}",
            status="open",
            occurred_at=now,
            week_id="",
            source_type="gmail",
            source_id=sid,
            source_item_id=row["message_id"],
            reportable=False,
            raw={"candidate_id": candidate_id_value},
        )
    return get_work_item(path, item_id)


def reject_email_candidate(path: Path, candidate_id_value: str) -> dict[str, Any] | None:
    init_db(path)
    now = utc_now()
    with connect(path) as conn:
        row = conn.execute(
            "SELECT candidate_id FROM email_candidates WHERE candidate_id=?",
            (candidate_id_value,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE email_candidates SET status='rejected', decision_reason='not_actionable', decided_at=? WHERE candidate_id=?",
            (now, candidate_id_value),
        )
    return get_email_candidate(path, candidate_id_value)


def done_email_candidate(path: Path, candidate_id_value: str) -> dict[str, Any] | None:
    init_db(path)
    now = utc_now()
    with connect(path) as conn:
        row = conn.execute(
            "SELECT candidate_id FROM email_candidates WHERE candidate_id=?",
            (candidate_id_value,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE email_candidates SET status='done', decision_reason='handled_elsewhere', decided_at=? WHERE candidate_id=?",
            (now, candidate_id_value),
        )
    return get_email_candidate(path, candidate_id_value)


def repair_email_candidate_bindings(
    path: Path,
    repairs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Atomically detach confirmed false Gmail bindings and restore parent state."""

    if not repairs:
        raise ValueError("missing_email_binding_repairs")
    init_db(path)
    now = utc_now()
    repaired: list[dict[str, Any]] = []
    context_keys = {
        "thread_id",
        "sender_intent",
        "context",
        "suggested_action",
        "gmail_url",
        "triage_model",
        "triage_confidence",
        "relationship",
        "existing_item_id",
        "existing_item_title",
        "suggested_training_label",
        "training_group_key",
        "training_group_count",
        "last_email_reply_at",
        "last_email_reply_message_id",
    }

    with connect(path) as conn:
        for spec in repairs:
            candidate_id_value = str(spec.get("candidate_id") or "").strip()
            expected_item_id = str(spec.get("expected_item_id") or "").strip()
            restore_status = str(spec.get("restore_status") or "").strip()
            restore_done_at = str(spec.get("restore_done_at") or "").strip() or None
            if not candidate_id_value or not expected_item_id:
                raise ValueError("invalid_email_binding_repair")
            if restore_status not in {"open", "awaiting_human", "blocked", "done"}:
                raise ValueError("invalid_email_binding_restore_status")
            if restore_status == "done" and not restore_done_at:
                raise ValueError("missing_email_binding_restore_done_at")

            row = conn.execute(
                """
                SELECT c.candidate_id, c.provider, c.message_id, c.thread_id,
                       c.status AS candidate_status, c.item_id, c.raw_json,
                       w.status AS item_status, w.metadata_json
                FROM email_candidates c
                JOIN work_items w ON w.item_id=c.item_id
                WHERE c.candidate_id=?
                """,
                (candidate_id_value,),
            ).fetchone()
            if not row:
                raise ValueError(f"email_binding_not_found:{candidate_id_value}")
            if str(row["item_id"] or "") != expected_item_id:
                raise ValueError(f"email_binding_target_changed:{candidate_id_value}")
            if str(row["candidate_status"] or "") != "accepted":
                raise ValueError(f"email_binding_not_accepted:{candidate_id_value}")

            message_id = str(row["message_id"] or "")
            provider = str(row["provider"] or "gmail")
            metadata = read_metadata(row["metadata_json"])
            if str(metadata.get("attention_message_id") or "") != message_id:
                raise ValueError(f"email_binding_attention_changed:{candidate_id_value}")

            previous_source = conn.execute(
                """
                SELECT s.source_id, s.uri
                FROM work_item_sources wis
                JOIN sources s ON s.source_id=wis.source_id
                WHERE wis.item_id=?
                  AND NOT (s.source_type=? AND wis.source_item_id=?)
                ORDER BY wis.last_seen_at DESC, wis.first_seen_at DESC
                LIMIT 1
                """,
                (expected_item_id, provider, message_id),
            ).fetchone()
            bad_source = conn.execute(
                """
                SELECT s.source_id
                FROM work_item_sources wis
                JOIN sources s ON s.source_id=wis.source_id
                WHERE wis.item_id=? AND s.source_type=? AND wis.source_item_id=?
                LIMIT 1
                """,
                (expected_item_id, provider, message_id),
            ).fetchone()
            if not bad_source:
                raise ValueError(f"email_binding_source_missing:{candidate_id_value}")
            bad_source_id = str(bad_source["source_id"])

            try:
                raw_payload = json.loads(str(row["raw_json"] or "{}"))
            except json.JSONDecodeError:
                raw_payload = {}
            if not isinstance(raw_payload, dict):
                raw_payload = {}
            raw_payload.update({
                "status": "proposed",
                "item_id": None,
                "existing_item_id": "",
                "existing_item_title": "",
                "relationship": "new_outcome",
                "reopen_existing": False,
                "decision_reason": "association_repaired",
            })
            raw_payload.pop("decided_at", None)

            for key in list(metadata):
                if key in context_keys or key.startswith("attention_"):
                    metadata.pop(key, None)

            conn.execute(
                """
                UPDATE email_candidates
                SET status='proposed', item_id=NULL, decided_at=NULL,
                    decision_reason='association_repaired', raw_json=?
                WHERE candidate_id=?
                """,
                (dumps(raw_payload), candidate_id_value),
            )
            conn.execute(
                """
                DELETE FROM work_item_sources
                WHERE item_id=? AND source_id=? AND source_item_id=?
                """,
                (expected_item_id, bad_source_id, message_id),
            )
            conn.execute(
                "DELETE FROM work_item_events WHERE event_id=?",
                (f"email_attention:{expected_item_id}:{message_id}",),
            )

            next_source_id = str(previous_source["source_id"]) if previous_source else None
            next_fingerprint = str(previous_source["uri"]) if previous_source else None
            done_at = restore_done_at if restore_status == "done" else None
            closed_at = restore_done_at if restore_status == "done" else None
            conn.execute(
                """
                UPDATE work_items
                SET status=?, done_at=?, closed_at=?, updated_at=?, last_seen_at=?,
                    current_source_id=?, source_fingerprint=?, metadata_json=?
                WHERE item_id=?
                """,
                (
                    restore_status,
                    done_at,
                    closed_at,
                    now,
                    now,
                    next_source_id,
                    next_fingerprint,
                    dumps(metadata),
                    expected_item_id,
                ),
            )
            if not conn.execute(
                "SELECT 1 FROM work_item_sources WHERE source_id=? LIMIT 1",
                (bad_source_id,),
            ).fetchone():
                conn.execute("DELETE FROM sources WHERE source_id=?", (bad_source_id,))

            upsert_work_item_event(
                conn,
                event_id=f"email_binding_repaired:{candidate_id_value}:{now}",
                item_id=expected_item_id,
                event_type="email_binding_repaired",
                category="",
                subject=candidate_id_value,
                title="Removed false Gmail association",
                summary=f"Detached {candidate_id_value} and restored the parent Outcome to {restore_status}.",
                status=restore_status,
                occurred_at=now,
                week_id="",
                source_type="cmd",
                source_id=None,
                source_item_id=message_id,
                reportable=False,
                raw={
                    "candidate_id": candidate_id_value,
                    "expected_item_id": expected_item_id,
                    "restore_status": restore_status,
                    "removed_source_id": bad_source_id,
                },
            )
            repaired.append({
                "candidate_id": candidate_id_value,
                "message_id": message_id,
                "item_id": expected_item_id,
                "restored_status": restore_status,
                "current_source_id": next_source_id,
            })

        verification_rows: list[dict[str, Any]] = []
        for result in repaired:
            verification = conn.execute(
                """
                SELECT c.status AS candidate_status, c.item_id,
                       w.status AS item_status, w.metadata_json,
                       EXISTS(
                         SELECT 1 FROM work_item_sources wis
                         WHERE wis.item_id=w.item_id AND wis.source_item_id=c.message_id
                       ) AS source_link_exists,
                       EXISTS(
                         SELECT 1 FROM work_item_events e
                         WHERE e.event_id='email_attention:' || w.item_id || ':' || c.message_id
                       ) AS attention_event_exists
                FROM email_candidates c
                CROSS JOIN work_items w
                WHERE c.candidate_id=? AND w.item_id=?
                """,
                (result["candidate_id"], result["item_id"]),
            ).fetchone()
            verified = bool(
                verification
                and verification["candidate_status"] == "proposed"
                and verification["item_id"] is None
                and verification["item_status"] == result["restored_status"]
                and not verification["source_link_exists"]
                and not verification["attention_event_exists"]
                and str(read_metadata(verification["metadata_json"]).get("attention_message_id") or "")
                   != result["message_id"]
            )
            verification_rows.append({**result, "verified": verified})
        if not all(row["verified"] for row in verification_rows):
            raise RuntimeError("email_binding_repair_verification_failed")

    return {
        "ok": True,
        "operation": "email_binding_repair",
        "repaired": repaired,
        "verification": {
            "verified": True,
            "count": len(repaired),
            "items": verification_rows,
        },
        "repaired_at": now,
    }


def label_email_candidate(path: Path, candidate_id_value: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Record the user's training label and apply the requested email route."""

    init_db(path)
    label = str(payload.get("label") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if label == "block":
        label = "block_pattern"
    valid_labels = {"promote", "attach", "fyi", "ignore", "block_pattern"}
    if label not in valid_labels:
        raise ValueError("invalid_training_label")

    rationale = str(payload.get("rationale") or "").strip()
    category = str(payload.get("category") or payload.get("proposed_category") or "").strip()
    priority = normalize_email_priority(str(payload.get("priority") or payload.get("proposed_urgency") or "low"))
    relationship = str(payload.get("relationship") or "").strip()
    item_id = str(payload.get("item_id") or "").strip()
    now = utc_now()

    if label == "promote":
        overrides = {
            key: payload[key]
            for key in ("proposed_title", "proposed_body", "proposed_category", "proposed_urgency")
            if key in payload
        }
        if category:
            overrides["proposed_category"] = category
        if priority:
            overrides["proposed_urgency"] = priority
        task = accept_email_candidate(path, candidate_id_value, overrides=overrides)
        if not task:
            return None
        with connect(path) as conn:
            update_email_training_fields(
                conn,
                candidate_id_value,
                label=label,
                rationale=rationale,
                category=category,
                priority=priority,
                relationship=relationship or "new_outcome",
                payload=payload,
                decision_reason=rationale or "promoted_to_task",
                decided_at=now,
            )
        return {"task": task, "candidate": get_email_candidate(path, candidate_id_value)}

    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM email_candidates WHERE candidate_id=?",
            (candidate_id_value,),
        ).fetchone()
        if not row:
            return None

        if label == "attach":
            if not item_id:
                raise ValueError("missing_item_id")
            work_item = conn.execute(
                "SELECT item_id, title FROM work_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if not work_item:
                raise ValueError("item_not_found")
            raw_candidate = read_metadata(row["raw_json"])
            source_uri = f"gmail:{row['message_id']}"
            sid = upsert_source(
                conn,
                "gmail",
                source_uri,
                row["subject"] or row["message_id"],
                {
                    "message_id": row["message_id"],
                    "thread_id": row["thread_id"],
                    "candidate_id": candidate_id_value,
                    "training_label": label,
                    **email_context_metadata(raw_candidate),
                },
            )
            conn.execute(
                """
                INSERT INTO work_item_sources(
                  item_id, source_id, source_item_id, first_seen_at, last_seen_at, raw_text
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id, source_id, source_item_id) DO UPDATE SET
                  last_seen_at=excluded.last_seen_at,
                  raw_text=excluded.raw_text
                """,
                (item_id, sid, row["message_id"], now, now, row["snippet"] or row["subject"] or ""),
            )
            conn.execute(
                """
                UPDATE work_items
                SET updated_at=?, last_seen_at=?
                WHERE item_id=?
                """,
                (now, now, item_id),
            )
            conn.execute(
                """
                UPDATE email_candidates
                SET status='accepted', item_id=?, decided_at=?
                WHERE candidate_id=?
                """,
                (item_id, now, candidate_id_value),
            )
            upsert_work_item_event(
                conn,
                event_id=f"email_candidate:{candidate_id_value}:attached",
                item_id=item_id,
                event_type="email_candidate_attached",
                category=category or row["proposed_category"] or "",
                subject=row["sender"] or row["subject"] or "",
                title=work_item["title"] or row["subject"] or "",
                summary=f"Attached Gmail candidate from {row['sender']}: {row['subject']}",
                status="open",
                occurred_at=now,
                week_id="",
                source_type="gmail",
                source_id=sid,
                source_item_id=row["message_id"],
                reportable=False,
                raw={"candidate_id": candidate_id_value, "training_label": label, "rationale": rationale},
            )
            decision_reason = rationale or "attached_to_existing_outcome"
            relationship = relationship or "continuation"
        elif label == "fyi":
            conn.execute(
                "UPDATE email_candidates SET status='done', decided_at=? WHERE candidate_id=?",
                (now, candidate_id_value),
            )
            decision_reason = rationale or "fyi_preserved"
            relationship = relationship or "none"
        elif label == "ignore":
            conn.execute(
                "UPDATE email_candidates SET status='rejected', decided_at=? WHERE candidate_id=?",
                (now, candidate_id_value),
            )
            decision_reason = rationale or "ignored_by_human"
            relationship = relationship or "none"
        else:
            conn.execute(
                "UPDATE email_candidates SET status='rejected', decided_at=? WHERE candidate_id=?",
                (now, candidate_id_value),
            )
            decision_reason = rationale or "block_pattern"
            relationship = relationship or "none"

        update_email_training_fields(
            conn,
            candidate_id_value,
            label=label,
            rationale=rationale,
            category=category,
            priority=priority,
            relationship=relationship,
            payload=payload,
            decision_reason=decision_reason,
            decided_at=now,
        )

    return {"candidate": get_email_candidate(path, candidate_id_value)}


def update_work_item(
    path: Path,
    item_id: str,
    changes: dict[str, Any],
    source: str = "ui",
    *,
    close_with_open_subtasks: bool = False,
) -> dict[str, Any] | None:
    """Apply a simple direct UI edit to the canonical task database."""

    allowed_statuses = {"open", "awaiting_human", "blocked", "done", "dropped", "cancelled", "superseded"}
    field_columns = {
        "title": "title",
        "body": "body",
        "detail": "body",
        "category": "category",
        "cat": "category",
        "urgency": "urgency",
        "today": "today",
        "status": "status",
    }

    normalized: dict[str, Any] = {}
    for key, value in changes.items():
        column = field_columns.get(key)
        if not column:
            continue
        normalized[column] = value

    if not normalized:
        return get_work_item(path, item_id)

    init_db(path)
    now = utc_now()
    with connect(path) as conn:
        row = conn.execute(
            "SELECT item_id, title, body, category, urgency, status, metadata_json FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if not row:
            return None

        requested_status = str(normalized.get("status") or "").strip()
        if requested_status == "done" and not close_with_open_subtasks:
            active_child = conn.execute(
                """
                SELECT 1
                FROM work_items
                WHERE parent_item_id=?
                  AND status IN ('open', 'awaiting_human', 'blocked')
                LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            if active_child:
                raise WorkItemHierarchyError("active_subtasks_remaining")

        metadata = read_metadata(row["metadata_json"])
        overrides = direct_overrides(metadata)
        set_parts = ["updated_at=?", "last_action_at=?"]
        params: list[Any] = [now, now]
        changed_fields: list[str] = []
        next_status = str(row["status"] or "open")

        for column, value in normalized.items():
            if column == "status":
                status = str(value or "").strip()
                if status not in allowed_statuses:
                    continue
                next_status = status
                set_parts.append("status=?")
                params.append(status)
                changed_fields.append("status")
                overrides.add("status")
                if status in {"done", "dropped", "cancelled", "superseded"}:
                    set_parts.append("closed_at=?")
                    params.append(now)
                    set_parts.append("today=0")
                    overrides.add("today")
                    if status == "done":
                        set_parts.append("done_at=?")
                        params.append(now)
                    elif status in {"dropped", "cancelled", "superseded"}:
                        set_parts.append("done_at=NULL")
                elif status == "open":
                    set_parts.append("closed_at=NULL")
                    set_parts.append("done_at=NULL")
                continue

            if column == "today":
                today_value = 1 if bool(value) else 0
                set_parts.append("today=?")
                params.append(today_value)
                changed_fields.append("today")
                overrides.add("today")
                continue

            text = str(value or "").strip()
            if column == "title" and not text:
                continue
            set_parts.append(f"{column}=?")
            params.append(text)
            changed_fields.append(column)
            overrides.add(column)

        if not changed_fields:
            return get_work_item(path, item_id)

        metadata["direct_overrides"] = sorted(overrides)
        metadata["last_direct_update_at"] = now
        metadata["last_direct_update_source"] = source
        metadata["last_direct_update_fields"] = changed_fields
        set_parts.append("metadata_json=?")
        params.append(dumps(metadata))
        params.append(item_id)
        conn.execute(f"UPDATE work_items SET {', '.join(set_parts)} WHERE item_id=?", params)

        updated = conn.execute(
            "SELECT title, body, category, status FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if updated:
            upsert_work_item_event(
                conn,
                event_id=f"direct:{item_id}:{now}",
                item_id=item_id,
                event_type="direct_update",
                category=str(updated["category"] or ""),
                subject=str(updated["title"] or "").split("—", 1)[0].strip(),
                title=str(updated["title"] or item_id),
                summary=f"Direct {source} update: {', '.join(changed_fields)}.",
                status=next_status,
                occurred_at=now,
                week_id="",
                source_type="ui",
                reportable=False,
                raw={"changes": changes, "fields": changed_fields},
            )

    return get_work_item(path, item_id)


def _sibling_ids(
    conn: sqlite3.Connection,
    parent_item_id: str | None,
    *,
    exclude_item_id: str = "",
) -> list[str]:
    parent_clause = "parent_item_id IS NULL" if parent_item_id is None else "parent_item_id=?"
    params: list[Any] = [] if parent_item_id is None else [parent_item_id]
    exclude_clause = ""
    if exclude_item_id:
        exclude_clause = " AND item_id<>?"
        params.append(exclude_item_id)
    rows = conn.execute(
        f"""
        SELECT item_id
        FROM work_items
        WHERE {parent_clause}{exclude_clause}
        ORDER BY sort_order, created_at, item_id
        """,
        params,
    ).fetchall()
    return [str(row["item_id"]) for row in rows]


def _write_sibling_order(conn: sqlite3.Connection, item_ids: list[str], now: str) -> None:
    for index, sibling_id in enumerate(item_ids):
        conn.execute(
            "UPDATE work_items SET sort_order=?, updated_at=? WHERE item_id=?",
            (index, now, sibling_id),
        )


def reparent_work_item(
    path: Path,
    item_id: str,
    parent_item_id: str | None,
    sort_order: int | None = None,
    *,
    source: str = "ui",
) -> dict[str, Any]:
    """Move a durable item without changing its identity or attached history."""

    init_db(path)
    item_id = str(item_id or "").strip()
    parent_item_id = str(parent_item_id or "").strip() or None
    now = utc_now()
    with connect(path) as conn:
        item = conn.execute(
            "SELECT item_id, title, category, status, parent_item_id FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if not item:
            raise WorkItemHierarchyError("item_not_found")
        if parent_item_id == item_id:
            raise WorkItemHierarchyError("cannot_parent_self")

        parent = None
        if parent_item_id is not None:
            parent = conn.execute(
                "SELECT item_id, parent_item_id FROM work_items WHERE item_id=?",
                (parent_item_id,),
            ).fetchone()
            if not parent:
                raise WorkItemHierarchyError("parent_not_found")

            ancestor_id: str | None = parent_item_id
            while ancestor_id is not None:
                if ancestor_id == item_id:
                    raise WorkItemHierarchyError("hierarchy_cycle")
                ancestor = conn.execute(
                    "SELECT parent_item_id FROM work_items WHERE item_id=?",
                    (ancestor_id,),
                ).fetchone()
                ancestor_id = str(ancestor["parent_item_id"]) if ancestor and ancestor["parent_item_id"] else None

            if parent["parent_item_id"] is not None:
                raise WorkItemHierarchyError("max_depth_exceeded")
            has_children = conn.execute(
                "SELECT 1 FROM work_items WHERE parent_item_id=? LIMIT 1",
                (item_id,),
            ).fetchone()
            if has_children:
                raise WorkItemHierarchyError("max_depth_exceeded")

        old_parent_id = str(item["parent_item_id"]) if item["parent_item_id"] else None
        old_siblings = _sibling_ids(conn, old_parent_id, exclude_item_id=item_id)
        new_siblings = old_siblings if old_parent_id == parent_item_id else _sibling_ids(
            conn,
            parent_item_id,
            exclude_item_id=item_id,
        )
        insertion_index = len(new_siblings) if sort_order is None else max(0, min(int(sort_order), len(new_siblings)))
        new_siblings.insert(insertion_index, item_id)

        if old_parent_id != parent_item_id:
            _write_sibling_order(conn, old_siblings, now)
        conn.execute(
            "UPDATE work_items SET parent_item_id=?, updated_at=?, last_action_at=? WHERE item_id=?",
            (parent_item_id, now, now, item_id),
        )
        _write_sibling_order(conn, new_siblings, now)
        upsert_work_item_event(
            conn,
            event_id=f"reparent:{item_id}:{now}",
            item_id=item_id,
            event_type="work_item_reparented",
            category=str(item["category"] or ""),
            subject=str(item["title"] or "").split("—", 1)[0].strip(),
            title=str(item["title"] or item_id),
            summary=f"Moved item under {parent_item_id or 'root'} at position {insertion_index}.",
            status=str(item["status"] or "open"),
            occurred_at=now,
            week_id="",
            source_type=source,
            reportable=False,
            raw={
                "old_parent_item_id": old_parent_id,
                "parent_item_id": parent_item_id,
                "sort_order": insertion_index,
            },
        )

    result = get_work_item(path, item_id)
    if result is None:  # The row was read and updated in the same transaction.
        raise WorkItemHierarchyError("item_not_found")
    return result


def sync_from_state(
    db_path: Path,
    *,
    weekly_log: Path,
    actions: list[dict[str, Any]],
    results: list[dict[str, Any]],
    dispatches: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    report_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Import append-only runtime history and weekly report events into SQLite.

    ``work_items`` is authoritative. Weekly Markdown contributes narrative Log
    events only; it never creates or updates operational task records.
    """

    init_db(db_path)
    now = utc_now()
    weekly_uri = str(weekly_log)
    week_id = week_id_from_path(weekly_log)
    week_start = week_start_at(weekly_log)
    with connect(db_path) as conn:
        weekly_source_id = upsert_source(
            conn,
            "weekly_log",
            weekly_uri,
            weekly_log.name,
            {"path": weekly_uri},
        )
        for index, entry in enumerate(report_entries or []):
            category = str(entry.get("category") or "")
            upsert_work_item_event(
                conn,
                event_id=str(entry.get("id") or f"weekly:{weekly_log.name}:{index + 1}"),
                item_id=entry.get("item_id"),
                event_type="weekly_log_entry",
                category=category,
                subject=str(entry.get("subject") or ""),
                title=str(entry.get("title") or ""),
                summary=str(entry.get("summary") or entry.get("body") or ""),
                status="completed",
                occurred_at=str(entry.get("occurred_at") or week_start),
                week_id=str(entry.get("week_id") or week_id),
                source_type="weekly_log",
                source_id=weekly_source_id,
                source_item_id=str(entry.get("source_item_id") or entry.get("id") or index + 1),
                raw=entry,
            )

        action_kind_by_id: dict[str, str] = {}
        action_item_by_id: dict[str, str | None] = {}
        action_binding_by_id: dict[str, dict[str, Any]] = {}
        for action in actions:
            action_id = str(action.get("id") or "")
            if not action_id:
                continue
            task = action.get("task") if isinstance(action.get("task"), dict) else {}
            resolver_authority = action.get("binding_authority") == "resolver"
            requested_item_id = str(
                (action.get("resolved_item_id") if resolver_authority else task.get("id")) or ""
            )
            item_id = requested_item_id if requested_item_id and conn.execute(
                "SELECT 1 FROM work_items WHERE item_id=?",
                (requested_item_id,),
            ).fetchone() else None
            exact_capture_lineage = False
            if item_id is None and not resolver_authority:
                item_id = infer_exact_active_capture_item_id(conn, action)
                exact_capture_lineage = item_id is not None
            action_metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
            operation = action_metadata.get("proposed_operation") if isinstance(action_metadata.get("proposed_operation"), dict) else {}
            risk = operation or action_metadata.get("risk") or {}
            action_kind = str(action.get("kind") or "")
            action_kind_by_id[action_id] = action_kind
            action_item_by_id[action_id] = item_id
            action_binding_by_id[action_id] = {
                "authority": (
                    "resolver" if resolver_authority
                    else "exact_capture_lineage" if exact_capture_lineage
                    else "legacy"
                ),
                "decision": action.get("binding_decision") or (
                    "attach_exact_capture" if exact_capture_lineage else None
                ),
                "resolved_item_id": item_id,
            }
            conn.execute(
                """
                INSERT INTO actions(
                  action_id, item_id, kind, origin, instruction, note, risk_level, risk_label,
                  status, created_at, updated_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                  item_id=excluded.item_id,
                  kind=excluded.kind,
                  origin=excluded.origin,
                  instruction=excluded.instruction,
                  note=excluded.note,
                  risk_level=excluded.risk_level,
                  risk_label=excluded.risk_label,
                  updated_at=excluded.updated_at,
                  raw_json=excluded.raw_json
                """,
                (
                    action_id,
                    item_id,
                    action_kind,
                    str(action.get("origin") or ""),
                    str(action.get("instruction") or ""),
                    str(action.get("note") or ""),
                    (risk.get("risk_level") or risk.get("level")) if isinstance(risk, dict) else None,
                    (risk.get("label") or risk.get("capability")) if isinstance(risk, dict) else None,
                    str(action.get("status") or "queued"),
                    str(action.get("time") or now),
                    now,
                    dumps(action),
                ),
            )
            instruction = str(action.get("instruction") or action.get("note") or "")
            conn.execute(
                """
                INSERT INTO turns(turn_id, item_id, action_id, actor_type, turn_type, content, created_at, metadata_json)
                VALUES (?, ?, ?, 'human', 'instruction', ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                  item_id=excluded.item_id,
                  content=excluded.content,
                  metadata_json=excluded.metadata_json
                """,
                (
                    f"turn:instruction:{action_id}",
                    item_id,
                    action_id,
                    instruction,
                    str(action.get("time") or now),
                    dumps({"kind": action_kind, "origin": action.get("origin"), "raw": action}),
                ),
            )
            if item_id:
                conn.execute(
                    """
                    INSERT INTO priority_signals(item_id, signal_type, weight, observed_at, source_action_id, metadata_json)
                    SELECT ?, 'agent_action', 1.0, ?, ?, ?
                    WHERE NOT EXISTS (
                      SELECT 1 FROM priority_signals
                      WHERE signal_type='agent_action' AND source_action_id=?
                    )
                    """,
                    (item_id, str(action.get("time") or now), action_id, dumps({"kind": action_kind}), action_id),
                )

        latest_receipt_by_action: dict[str, dict[str, Any]] = {}
        for result in results:
            action_id = str(result.get("action_id") or "")
            if not action_id:
                continue
            binding = action_binding_by_id.get(action_id) or {"authority": "legacy"}
            item_id = action_item_by_id.get(action_id)
            projected_result = dict(result)
            if binding["authority"] in {"resolver", "exact_capture_lineage"}:
                worker_item_id = infer_result_item_id(conn, result)
                if worker_item_id != item_id and worker_item_id is not None:
                    projected_result["binding_mismatch"] = {
                        "binding_authority": binding["authority"],
                        "binding_decision": binding.get("decision"),
                        "resolved_item_id": item_id,
                        "worker_item_id": worker_item_id,
                    }
            latest_receipt_by_action[action_id] = projected_result
            effective_status = effective_result_status(result)
            receipt_summary = str(result.get("summary") or "")
            if result_blocks_on_google_doc_contract(result):
                receipt_summary = google_doc_contract_summary(result, receipt_summary)
            receipt_cursor = conn.execute(
                """
                INSERT INTO receipts(action_id, status, effective_status, summary, model, created_at, raw_json)
                SELECT ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                  SELECT 1 FROM receipts
                  WHERE action_id=? AND status=? AND summary=? AND created_at=?
                )
                """,
                (
                    action_id,
                    str(result.get("status") or ""),
                    effective_status,
                    receipt_summary,
                    result.get("model"),
                    str(result.get("time") or result.get("timestamp") or now),
                    dumps(projected_result),
                    action_id,
                    str(result.get("status") or ""),
                    receipt_summary,
                    str(result.get("time") or result.get("timestamp") or now),
                ),
            )
            receipt_is_new = receipt_cursor.rowcount > 0
            conn.execute(
                "UPDATE actions SET status=?, updated_at=? WHERE action_id=?",
                (effective_status, now, action_id),
            )
            result_time = str(result.get("time") or result.get("timestamp") or now)
            inferred_item_id = (
                None
                if binding["authority"] in {"resolver", "exact_capture_lineage"}
                else infer_result_item_id(conn, result, require_active=item_id is None)
            )
            if inferred_item_id and inferred_item_id != item_id:
                item_id = inferred_item_id
                action_item_by_id[action_id] = item_id
                conn.execute("UPDATE actions SET item_id=? WHERE action_id=?", (item_id, action_id))
                conn.execute("UPDATE turns SET item_id=? WHERE action_id=?", (item_id, action_id))
            if receipt_marks_reviewed(result):
                conn.execute(
                    "UPDATE actions SET reviewed_at=COALESCE(reviewed_at, ?) WHERE action_id=?",
                    (result_time, action_id),
                )
            human_resolution = receipt_marks_reviewed(result) or result.get("record_type") == "human_resolution"
            response_content = str(result.get("conclusion") or receipt_summary or "")
            response_turn_id = f"turn:response:{action_id}:{result_time}"
            conn.execute(
                """
                INSERT INTO turns(turn_id, item_id, action_id, actor_type, turn_type, content, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                  item_id=excluded.item_id,
                  actor_type=excluded.actor_type,
                  turn_type=excluded.turn_type,
                  content=excluded.content,
                  metadata_json=excluded.metadata_json
                """,
                (
                    response_turn_id,
                    item_id,
                    action_id,
                    "human" if human_resolution else "agent",
                    "acknowledgement" if human_resolution else "response",
                    response_content,
                    result_time,
                    dumps({"status": effective_status, "summary": result.get("summary"), "model": result.get("model"), "raw": result}),
                ),
            )
            artifact_value = result.get("artifact")
            if isinstance(artifact_value, dict):
                artifact_content = str(artifact_value.get("content") or artifact_value.get("body") or artifact_value.get("text") or "")
                artifact_type = str(artifact_value.get("type") or "agent_output")
                artifact_label = str(artifact_value.get("title") or artifact_value.get("label") or "Agent output")
                artifact_uri = artifact_value.get("url") or artifact_value.get("uri")
                artifact_external_id = artifact_value.get("external_id")
            else:
                artifact_content = str(artifact_value or result.get("draft") or result.get("content") or "")
                artifact_type = "agent_output"
                artifact_label = "Agent output"
                artifact_uri = None
                artifact_external_id = None
            if artifact_content or artifact_uri:
                artifact_id = f"artifact:{action_id}:{result_time}"
                previous = conn.execute(
                    "SELECT artifact_id, version FROM artifacts WHERE item_id IS ? AND artifact_type=? ORDER BY created_at DESC, artifact_id DESC LIMIT 1",
                    (item_id, artifact_type),
                ).fetchone()
                version = int(previous["version"] or 1) + 1 if previous and previous["artifact_id"] != artifact_id else int(previous["version"] or 1) if previous else 1
                supersedes = previous["artifact_id"] if previous and previous["artifact_id"] != artifact_id else None
                conn.execute(
                    """
                    INSERT INTO artifacts(
                      artifact_id, action_id, item_id, turn_id, artifact_type, label, uri, external_id,
                      status, content, version, supersedes_artifact_id, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                      item_id=excluded.item_id,
                      turn_id=excluded.turn_id,
                      content=excluded.content,
                      uri=excluded.uri,
                      external_id=excluded.external_id,
                      version=excluded.version,
                      supersedes_artifact_id=excluded.supersedes_artifact_id,
                      updated_at=excluded.updated_at,
                      metadata_json=excluded.metadata_json
                    """,
                    (
                        artifact_id, action_id, item_id, response_turn_id, artifact_type,
                        artifact_label, artifact_uri, artifact_external_id, artifact_content, version, supersedes,
                        result_time, now, dumps({"sources": result.get("sources") or [], "raw": artifact_value}),
                    ),
                )
            if item_id and receipt_is_new:
                current_row = conn.execute(
                    """
                    SELECT status, category, title, metadata_json,
                           closed_at, done_at, last_action_at
                    FROM work_items
                    WHERE item_id=?
                    """,
                    (item_id,),
                ).fetchone()
                current = current_row["status"] if current_row else "open"
                category = current_row["category"] if current_row else ""
                title = current_row["title"] if current_row else item_id
                current_metadata = read_metadata(current_row["metadata_json"]) if current_row else {}
                if "status" in direct_overrides(current_metadata) and is_later_iso(current_metadata.get("last_direct_update_at"), result_time):
                    next_status = current
                else:
                    next_status = infer_item_status(
                        current,
                        action_kind_by_id.get(action_id, ""),
                        effective_status,
                        str(result.get("summary") or ""),
                        needs_human_review=bool(result.get("needs_human_review")),
                    )
                closed_at = result_time if next_status in {"done", "dropped", "cancelled", "superseded"} else None
                done_at = result_time if next_status == "done" else None
                next_closed_at = closed_at or current_row["closed_at"]
                next_done_at = done_at or current_row["done_at"]
                if (
                    next_status != current
                    or result_time != current_row["last_action_at"]
                    or next_closed_at != current_row["closed_at"]
                    or next_done_at != current_row["done_at"]
                ):
                    conn.execute(
                        """
                        UPDATE work_items
                        SET status=?,
                            updated_at=?,
                            last_action_at=?,
                            closed_at=?,
                            done_at=?
                        WHERE item_id=?
                        """,
                        (next_status, now, result_time, next_closed_at, next_done_at, item_id),
                    )
                if effective_status == "completed":
                    event_type = "action_reviewed" if human_resolution else "action_receipt"
                    upsert_work_item_event(
                        conn,
                        event_id=f"receipt:{action_id}:{result_time}",
                        item_id=item_id,
                        event_type=event_type,
                        category=category,
                        subject=title.split("—", 1)[0].strip(),
                        title=title,
                        summary=str(result.get("summary") or ""),
                        status=next_status,
                        occurred_at=result_time,
                        week_id=week_id,
                        source_type="receipt",
                        source_action_id=action_id,
                        reportable=False,
                        raw=result,
                    )

        for dispatch in dispatches:
            dispatch_id = str(dispatch.get("id") or "")
            if not dispatch_id:
                continue
            conn.execute(
                """
                INSERT INTO dispatches(dispatch_id, mode, status, created_at, note, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dispatch_id) DO UPDATE SET
                  mode=excluded.mode,
                  status=excluded.status,
                  note=excluded.note,
                  raw_json=excluded.raw_json
                """,
                (
                    dispatch_id,
                    str(dispatch.get("mode") or ""),
                    str(dispatch.get("status") or ""),
                    str(dispatch.get("time") or now),
                    str(dispatch.get("note") or ""),
                    dumps(dispatch),
                ),
            )
            conn.execute(
                """
                INSERT INTO runs(run_id, dispatch_id, run_type, status, started_at, metadata_json)
                VALUES (?, ?, 'agent', ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status=excluded.status,
                  metadata_json=excluded.metadata_json
                """,
                (
                    f"run:{dispatch_id}",
                    dispatch_id,
                    str(dispatch.get("status") or "queued"),
                    str(dispatch.get("time") or now),
                    dumps(dispatch),
                ),
            )
            for action_id in dispatch.get("action_ids") or []:
                conn.execute(
                    "INSERT OR IGNORE INTO dispatch_actions(dispatch_id, action_id) VALUES (?, ?)",
                    (dispatch_id, str(action_id)),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO run_actions(run_id, action_id) VALUES (?, ?)",
                    (f"run:{dispatch_id}", str(action_id)),
                )

        for approval in approvals:
            action_id = str(approval.get("action_id") or "")
            if not action_id:
                continue
            conn.execute(
                """
                INSERT INTO approvals(action_id, approved, created_at, summary, raw_json)
                SELECT ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                  SELECT 1 FROM approvals WHERE action_id=? AND created_at=?
                )
                """,
                (
                    action_id,
                    1 if approval.get("approved") else 0,
                    str(approval.get("time") or now),
                    str(approval.get("summary") or ""),
                    dumps(approval),
                    action_id,
                    str(approval.get("time") or now),
                ),
            )
            item_id = action_item_by_id.get(action_id)
            approval_time = str(approval.get("time") or now)
            conn.execute(
                """
                INSERT OR IGNORE INTO turns(
                  turn_id, item_id, action_id, actor_type, turn_type, content, created_at, metadata_json
                ) VALUES (?, ?, ?, 'human', 'decision', ?, ?, ?)
                """,
                (
                    f"turn:decision:{action_id}:{approval_time}",
                    item_id,
                    action_id,
                    str(approval.get("summary") or ("Approved" if approval.get("approved") else "Not approved")),
                    approval_time,
                    dumps(approval),
                ),
            )

        counts = {
            "work_items": conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
            "open_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='open'").fetchone()[0],
            "awaiting_human_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='awaiting_human'").fetchone()[0],
            "done_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='done'").fetchone()[0],
            "dropped_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='dropped'").fetchone()[0],
            "actions": conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0],
            "receipts": conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
            "turns": conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "artifacts": conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            "dispatches": conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone()[0],
            "reportable_events": conn.execute("SELECT COUNT(*) FROM work_item_events WHERE reportable=1").fetchone()[0],
        }
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_sync_at', ?)", (now,))
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('last_sync_counts', ?)", (dumps(counts),))
        return counts


def db_summary(path: Path) -> dict[str, Any]:
    init_db(path)
    with connect(path) as conn:
        meta = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}
        counts = {
            "work_items": conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
            "open_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='open'").fetchone()[0],
            "awaiting_human_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='awaiting_human'").fetchone()[0],
            "done_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='done'").fetchone()[0],
            "dropped_items": conn.execute("SELECT COUNT(*) FROM work_items WHERE status='dropped'").fetchone()[0],
            "actions": conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0],
            "receipts": conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
            "turns": conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0],
            "artifacts": conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            "dispatches": conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone()[0],
            "reportable_events": conn.execute("SELECT COUNT(*) FROM work_item_events WHERE reportable=1").fetchone()[0],
        }
    return {"path": str(path), "meta": meta, "counts": counts}


def list_report_events(
    path: Path,
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    categories: set[str] | None = None,
) -> list[dict[str, Any]]:
    init_db(path)
    categories = categories or REPORTABLE_CATEGORIES
    clauses = ["e.reportable=1"]
    params: list[Any] = []
    if start_at:
        clauses.append("e.occurred_at >= ?")
        params.append(start_at)
    if end_at:
        clauses.append("e.occurred_at <= ?")
        params.append(end_at)
    if categories:
        clauses.append("e.category IN ({})".format(",".join("?" for _ in categories)))
        params.extend(sorted(categories))

    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              e.event_id,
              e.item_id,
              e.event_type,
              e.category,
              e.subject,
              e.title,
              e.summary,
              e.status,
              e.occurred_at,
              e.week_id,
              e.source_type,
              e.source_id,
              e.source_item_id,
              e.source_action_id,
              wi.status AS item_status,
              wi.done_at,
              wi.closed_at
            FROM work_item_events e
            LEFT JOIN work_items wi ON wi.item_id = e.item_id
            WHERE {" AND ".join(clauses)}
            ORDER BY e.occurred_at, e.category, e.subject COLLATE NOCASE, e.title COLLATE NOCASE
            """,
            params,
        ).fetchall()

    return [
        {
            "event_id": row["event_id"],
            "item_id": row["item_id"],
            "event_type": row["event_type"],
            "category": row["category"],
            "subject": row["subject"],
            "title": row["title"],
            "summary": row["summary"],
            "status": row["status"],
            "occurred_at": row["occurred_at"],
            "week_id": row["week_id"],
            "source_type": row["source_type"],
            "source_id": row["source_id"],
            "source_item_id": row["source_item_id"],
            "source_action_id": row["source_action_id"],
            "item_status": row["item_status"],
            "done_at": row["done_at"],
            "closed_at": row["closed_at"],
        }
        for row in rows
    ]


def list_report_done_items(
    path: Path,
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    categories: set[str] | None = None,
) -> list[dict[str, Any]]:
    init_db(path)
    categories = categories or REPORTABLE_CATEGORIES
    clauses = ["wi.status='done'", "wi.done_at IS NOT NULL", "wi.parent_item_id IS NULL"]
    params: list[Any] = []
    if start_at:
        clauses.append("wi.done_at >= ?")
        params.append(start_at)
    if end_at:
        clauses.append("wi.done_at <= ?")
        params.append(end_at)
    if categories:
        clauses.append("wi.category IN ({})".format(",".join("?" for _ in categories)))
        params.extend(sorted(categories))

    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              wi.item_id,
              wi.title,
              wi.body,
              wi.category,
              wi.urgency,
              wi.today,
              wi.status,
              wi.done_at,
              wi.closed_at,
              wi.last_action_at,
              wi.source_state,
              wi.metadata_json,
              s.source_type,
              s.uri AS source_uri,
              s.label AS source_label,
              a.action_id AS latest_action_id,
              a.kind AS latest_action_kind,
              r.summary AS latest_receipt_summary,
              r.created_at AS latest_receipt_at
            FROM work_items wi
            LEFT JOIN sources s ON s.source_id = wi.current_source_id
            LEFT JOIN actions a ON a.action_id = (
              SELECT action_id
              FROM actions
              WHERE item_id = wi.item_id
              ORDER BY updated_at DESC, created_at DESC, action_id DESC
              LIMIT 1
            )
            LEFT JOIN receipts r ON r.receipt_id = (
              SELECT receipt_id
              FROM receipts
              WHERE action_id = a.action_id
              ORDER BY created_at DESC, receipt_id DESC
              LIMIT 1
            )
            WHERE {" AND ".join(clauses)}
            ORDER BY wi.done_at, wi.category, wi.title COLLATE NOCASE
            """,
            params,
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        task_meta = metadata.get("task") if isinstance(metadata.get("task"), dict) else {}
        body = row["body"] or task_meta.get("detail") or metadata.get("detail") or ""
        category = row["category"] or task_meta.get("category") or task_meta.get("cat") or ""
        subject = row["title"].split("—", 1)[0].strip()
        candidate = report_candidate_fields(
            category,
            row["title"],
            body,
            row["latest_receipt_summary"],
        )
        items.append({
            "item_id": row["item_id"],
            "title": row["title"],
            "subject": subject,
            "body": body,
            "category": category,
            "urgency": row["urgency"],
            "status": row["status"],
            "done_at": row["done_at"],
            "closed_at": row["closed_at"],
            "last_action_at": row["last_action_at"],
            "source_state": row["source_state"],
            "source_type": row["source_type"],
            "source_uri": row["source_uri"],
            "source_label": row["source_label"],
            "latest_action_id": row["latest_action_id"],
            "latest_action_kind": row["latest_action_kind"],
            "latest_receipt_summary": row["latest_receipt_summary"],
            "latest_receipt_at": row["latest_receipt_at"],
            **candidate,
        })
    return items


def list_weekly_report_activity(
    path: Path,
    *,
    start_at: str | None = None,
    end_at: str | None = None,
    categories: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return firm work completed during the window, even under open Outcomes.

    CMD Outcomes are durable and often remain open across several meaningful
    developments. Weekly reporting therefore keys off completed agent receipts,
    not only root Outcome closure. Human queue acknowledgements are workflow
    noise and are omitted. Child-item receipts are collapsed into their root
    Outcome so the report writer sees one coherent activity thread.
    """
    init_db(path)
    categories = categories or REPORTABLE_CATEGORIES
    clauses = ["r.effective_status='completed'", "a.item_id IS NOT NULL"]
    params: list[Any] = []
    if start_at:
        clauses.append("r.created_at >= ?")
        params.append(start_at)
    if end_at:
        clauses.append("r.created_at <= ?")
        params.append(end_at)
    if categories:
        clauses.append(
            "COALESCE(parent.category, wi.category) IN ({})".format(
                ",".join("?" for _ in categories)
            )
        )
        params.extend(sorted(categories))

    with connect(path) as conn:
        rows = conn.execute(
            f"""
            SELECT
              COALESCE(parent.item_id, wi.item_id) AS root_item_id,
              COALESCE(parent.title, wi.title) AS root_title,
              COALESCE(parent.body, wi.body) AS root_body,
              COALESCE(parent.category, wi.category) AS root_category,
              COALESCE(parent.urgency, wi.urgency) AS root_urgency,
              COALESCE(parent.status, wi.status) AS root_status,
              COALESCE(parent.done_at, wi.done_at) AS root_done_at,
              COALESCE(parent.closed_at, wi.closed_at) AS root_closed_at,
              wi.item_id AS activity_item_id,
              wi.title AS activity_item_title,
              a.action_id,
              a.kind AS action_kind,
              a.instruction AS action_instruction,
              r.receipt_id,
              r.status AS receipt_status,
              r.effective_status,
              r.summary,
              r.created_at,
              r.raw_json
            FROM receipts r
            JOIN actions a ON a.action_id=r.action_id
            JOIN work_items wi ON wi.item_id=a.item_id
            LEFT JOIN work_items parent ON parent.item_id=wi.parent_item_id
            WHERE {" AND ".join(clauses)}
            ORDER BY r.created_at, r.receipt_id
            """,
            params,
        ).fetchall()

    outcomes: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if receipt_marks_reviewed(raw):
            continue
        summary = str(row["summary"] or "").strip()
        conclusion = str(raw.get("conclusion") or "").strip()
        if not summary and not conclusion:
            continue

        root_item_id = str(row["root_item_id"])
        outcome = outcomes.setdefault(root_item_id, {
            "item_id": root_item_id,
            "title": row["root_title"],
            "subject": str(row["root_title"]).split("—", 1)[0].strip(),
            "body": row["root_body"] or "",
            "category": row["root_category"],
            "urgency": row["root_urgency"],
            "status": row["root_status"],
            "done_at": row["root_done_at"],
            "closed_at": row["root_closed_at"],
            "completed_in_window": False,
            "first_activity_at": row["created_at"],
            "last_activity_at": row["created_at"],
            "activities": [],
            "report_source": "cmd_receipts",
        })
        outcome["last_activity_at"] = row["created_at"]
        outcome["activities"].append({
            "receipt_id": row["receipt_id"],
            "action_id": row["action_id"],
            "action_kind": row["action_kind"],
            "action_instruction": row["action_instruction"],
            "item_id": row["activity_item_id"],
            "item_title": row["activity_item_title"],
            "occurred_at": row["created_at"],
            "summary": summary,
            "conclusion": conclusion,
            "receipt_status": row["receipt_status"],
            "effective_status": row["effective_status"],
        })

    completed = list_report_done_items(
        path,
        start_at=start_at,
        end_at=end_at,
        categories=categories,
    )
    for item in completed:
        outcome = outcomes.setdefault(item["item_id"], {
            "item_id": item["item_id"],
            "title": item["title"],
            "subject": item["subject"],
            "body": item["body"],
            "category": item["category"],
            "urgency": item["urgency"],
            "status": item["status"],
            "done_at": item["done_at"],
            "closed_at": item["closed_at"],
            "completed_in_window": True,
            "first_activity_at": item["done_at"],
            "last_activity_at": item["done_at"],
            "activities": [],
            "report_source": "cmd_outcome_completion",
        })
        outcome["completed_in_window"] = True
        outcome["done_at"] = item["done_at"]
        outcome["closed_at"] = item["closed_at"]
        if outcome["report_source"] == "cmd_receipts":
            outcome["report_source"] = "cmd_receipts+outcome_completion"

    result: list[dict[str, Any]] = []
    for outcome in outcomes.values():
        receipt_context = " ".join(
            part
            for activity in outcome["activities"]
            for part in (activity["summary"], activity["conclusion"])
            if part
        )
        candidate = report_candidate_fields(
            outcome["category"],
            outcome["title"],
            outcome["body"],
            receipt_context,
        )
        result.append({
            **outcome,
            "activity_count": len(outcome["activities"]),
            **candidate,
        })

    return sorted(
        result,
        key=lambda item: (
            item["last_activity_at"] or item["done_at"] or "",
            item["category"],
            item["title"].casefold(),
        ),
    )


def list_work_items(path: Path) -> list[dict[str, Any]]:
    init_db(path)
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              wi.item_id,
              wi.title,
              wi.body,
              wi.category,
              wi.urgency,
              wi.today,
              wi.status,
              wi.source_state,
              wi.last_seen_at,
              wi.last_action_at,
              wi.updated_at,
              wi.parent_item_id,
              wi.sort_order,
              parent.title AS parent_title,
              wi.metadata_json,
              s.source_type,
              s.label AS source_label,
              s.uri AS source_uri,
              a.action_id AS latest_action_id,
              a.kind AS latest_action_kind,
              r.effective_status AS latest_receipt_status,
              r.summary AS latest_receipt_summary,
              r.created_at AS latest_receipt_at
            FROM work_items wi
            LEFT JOIN work_items parent ON parent.item_id = wi.parent_item_id
            LEFT JOIN sources s ON s.source_id = wi.current_source_id
            LEFT JOIN actions a ON a.action_id = (
              SELECT action_id
              FROM actions
              WHERE item_id = wi.item_id
              ORDER BY updated_at DESC, created_at DESC, action_id DESC
              LIMIT 1
            )
            LEFT JOIN receipts r ON r.receipt_id = (
              SELECT receipt_id
              FROM receipts
              WHERE action_id = a.action_id
              ORDER BY created_at DESC, receipt_id DESC
              LIMIT 1
            )
            ORDER BY
              CASE wi.status
                WHEN 'awaiting_human' THEN 0
                WHEN 'blocked' THEN 1
                WHEN 'open' THEN 2
                WHEN 'done' THEN 3
                WHEN 'dropped' THEN 4
                WHEN 'cancelled' THEN 5
                WHEN 'superseded' THEN 6
                ELSE 7
              END,
              CASE LOWER(COALESCE(wi.urgency, ''))
                WHEN 'red' THEN 0
                WHEN 'high' THEN 0
                WHEN 'urgent' THEN 0
                WHEN 'yellow' THEN 1
                WHEN 'medium' THEN 1
                WHEN 'med' THEN 1
                ELSE 2
              END,
              COALESCE(wi.last_action_at, wi.last_seen_at, wi.updated_at) DESC,
              wi.title COLLATE NOCASE
            """
        ).fetchall()

    tasks: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        source_label = row["source_label"] or (metadata.get("source") if row["source_type"] else "command-db") or "command-db"
        source = f"weekly-log/{source_label}" if source_label.endswith(".md") else str(source_label)
        category = row["category"] or metadata.get("cat") or "uncategorized"
        urgency = row["urgency"] or metadata.get("urgency") or "low"
        detail = row["body"] or metadata.get("detail") or ""
        status = row["status"] or "open"
        tasks.append({
            "id": row["item_id"],
            "cat": category,
            "urgency": urgency,
            "title": row["title"],
            "detail": detail,
            "done": "Done means this work item is resolved and recorded in the command.",
            "today": bool(row["today"]) and status == "open",
            "source": source,
            "sourcePath": row["source_uri"] or "",
            "sourceUrl": metadata.get("gmail_url") or "",
            "emailSender": metadata.get("sender") or "",
            "emailSubject": metadata.get("subject") or "",
            "emailSenderIntent": metadata.get("sender_intent") or "",
            "emailContext": metadata.get("context") or "",
            "emailSuggestedAction": metadata.get("suggested_action") or "",
            "triageModel": metadata.get("triage_model") or "",
            "triageConfidence": metadata.get("triage_confidence"),
            "attentionKind": metadata.get("attention_kind") or "",
            "attentionLabel": metadata.get("attention_label") or "",
            "attentionSummary": metadata.get("attention_summary") or "",
            "attentionAt": metadata.get("attention_at") or "",
            "attentionSource": metadata.get("attention_source") or "",
            "attentionSender": metadata.get("attention_sender") or "",
            "attentionSubject": metadata.get("attention_subject") or "",
            "attentionUrl": metadata.get("attention_url") or "",
            "section": metadata.get("section") or "",
            "canonical": True,
            "dbStatus": status,
            "sourceState": row["source_state"],
            "lastActionAt": row["last_action_at"],
            "updatedAt": row["updated_at"],
            "latestActionId": row["latest_action_id"],
            "latestActionKind": row["latest_action_kind"],
            "latestReceiptStatus": row["latest_receipt_status"],
            "latestReceiptSummary": row["latest_receipt_summary"],
            "latestReceiptAt": row["latest_receipt_at"],
            "parentId": row["parent_item_id"],
            "sortOrder": int(row["sort_order"] or 0),
            "parentTitle": row["parent_title"] or "",
        })
    return tasks


def list_outcomes(path: Path) -> list[dict[str, Any]]:
    """Return root outcomes with their single supported level of subtasks."""

    flat_items = list_work_items(path)
    flat_rank = {str(item["id"]): index for index, item in enumerate(flat_items)}
    roots = [dict(item) for item in flat_items if not item.get("parentId")]
    children_by_parent: dict[str, list[dict[str, Any]]] = {}
    for item in flat_items:
        parent_id = str(item.get("parentId") or "")
        if parent_id:
            children_by_parent.setdefault(parent_id, []).append(dict(item))

    roots.sort(key=lambda item: (int(item.get("sortOrder") or 0), flat_rank[str(item["id"])]))
    result: list[dict[str, Any]] = []
    for root in roots:
        subtasks = children_by_parent.get(str(root["id"]), [])
        subtasks.sort(
            key=lambda item: (
                int(item.get("sortOrder") or 0),
                str(item.get("title") or "").casefold(),
                str(item.get("id") or ""),
            )
        )
        done_count = sum(1 for item in subtasks if item.get("dbStatus") == "done")
        active_count = sum(
            1
            for item in subtasks
            if item.get("dbStatus") in {"open", "awaiting_human", "blocked"}
        )
        root["subtasks"] = subtasks
        root["subtaskProgress"] = {
            "done": done_count,
            "total": done_count + active_count,
            "active": active_count,
        }
        result.append(root)
    return result


def list_agent_queue(path: Path) -> list[dict[str, Any]]:
    init_db(path)
    processed = {"done", "dropped", "cancelled", "superseded"}
    rows: list[sqlite3.Row]
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT
              a.action_id,
              a.item_id,
              a.kind,
              a.origin,
              a.instruction,
              a.note,
              a.risk_level,
              a.risk_label,
              a.status AS action_status,
              a.reviewed_at,
              a.raw_json AS action_raw_json,
              a.created_at,
              a.updated_at,
              wi.title AS task_title,
              wi.category,
              wi.status AS item_status,
              wi.source_state,
              r.receipt_id,
              r.status AS receipt_status,
              r.effective_status AS receipt_effective_status,
              r.summary AS receipt_summary,
              r.model,
              d.mode AS dispatch_mode,
              d.created_at AS dispatch_time
            FROM actions a
            LEFT JOIN work_items wi ON wi.item_id = a.item_id
            LEFT JOIN receipts r ON r.receipt_id = (
              SELECT receipt_id
              FROM receipts
              WHERE action_id = a.action_id
              ORDER BY created_at DESC, receipt_id DESC
              LIMIT 1
            )
            LEFT JOIN dispatch_actions da ON da.action_id = a.action_id
            LEFT JOIN dispatches d ON d.dispatch_id = (
              SELECT dispatch_id
              FROM dispatch_actions
              WHERE action_id = a.action_id
              ORDER BY dispatch_id DESC
              LIMIT 1
            )
            WHERE a.kind NOT IN ('focus', 'resume', 'return_note')
            ORDER BY a.created_at DESC, a.action_id DESC
            LIMIT 300
            """
        ).fetchall()

    consumed_preview_ids: set[str] = set()
    raw_action_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            raw_action = json.loads(row["action_raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            raw_action = {}
        raw_action_by_id[str(row["action_id"] or "")] = raw_action
        metadata = raw_action.get("metadata") if isinstance(raw_action.get("metadata"), dict) else {}
        operation = metadata.get("proposed_operation") if isinstance(metadata.get("proposed_operation"), dict) else {}
        preview_id = metadata.get("approved_preview_action_id") or operation.get("approved_from_action_id")
        if preview_id:
            consumed_preview_ids.add(str(preview_id))

    eligible_rows = [
        row for row in rows
        if row["action_id"] not in consumed_preview_ids and not row["reviewed_at"]
    ]
    queue: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_work: set[str] = set()
    attention_work_keys = {
        row["item_id"] or f"action:{row['action_id']}"
        for row in eligible_rows
        if agent_queue_row_needs_attention(row)
    }
    terminal_receipt_by_item: dict[str, int] = {}
    for row in eligible_rows:
        if (
            row["item_id"]
            and row["kind"] in {"done", "drop"}
            and (row["receipt_effective_status"] or row["receipt_status"] or "") == "completed"
        ):
            terminal_receipt_by_item[str(row["item_id"])] = max(
                terminal_receipt_by_item.get(str(row["item_id"]), 0),
                int(row["receipt_id"] or 0),
            )
    for row in eligible_rows:
        action_id = row["action_id"]
        if not action_id or action_id in seen:
            continue
        seen.add(action_id)
        work_key = row["item_id"] or f"action:{action_id}"
        row_needs_attention = agent_queue_row_needs_attention(row)
        # Coalesce routine sibling actions, but never let one of them hide an
        # unresolved approval, blocked result, or human-review artifact for
        # the same work item. Those are per-action obligations.
        if work_key in seen_work and not row_needs_attention:
            continue
        if work_key in attention_work_keys and not row_needs_attention:
            continue
        item_status = row["item_status"] or ""
        receipt_status = row["receipt_effective_status"] or row["receipt_status"] or ""
        raw_action = raw_action_by_id.get(str(action_id), {})
        completed_answer = (
            receipt_status == "completed"
            and row["kind"] in {"agent_chat", "quick_action", "codex_action"}
        )
        terminal_receipt_id = terminal_receipt_by_item.get(str(row["item_id"] or ""), 0)
        completed_after_terminal = completed_answer and (
            not terminal_receipt_id or int(row["receipt_id"] or 0) > terminal_receipt_id
        )
        if item_status in processed and not row_needs_attention and not completed_after_terminal:
            continue
        if receipt_status in {"cancelled", "dismissed"}:
            if work_key not in attention_work_keys:
                seen_work.add(work_key)
            continue
        if receipt_status == "completed" and item_status != "awaiting_human" and not completed_answer:
            if (
                work_key not in attention_work_keys
                and completed_receipt_suppresses_prior_attention(row["receipt_summary"] or "")
            ):
                seen_work.add(work_key)
            continue

        label = "Queued"
        state = "queued"
        needs_attention = False
        if receipt_status in {"blocked", "failed"}:
            if result_needs_normal_browser(row["receipt_summary"] or ""):
                label = "Use browser"
                state = "awaiting_human"
                needs_attention = True
            else:
                label = "Blocked"
                state = "blocked"
                needs_attention = True
        elif receipt_status == "awaiting_approval":
            label = "Approval needed"
            state = "approval"
            needs_attention = True
        elif row["action_status"] == "needs_clarification":
            label = "Clarification needed"
            state = "awaiting_human"
            needs_attention = True
        elif item_status == "awaiting_human":
            label = "Review needed"
            state = "awaiting_human"
            needs_attention = True
        elif completed_answer:
            label = "Agent answered"
            state = "awaiting_human"
            needs_attention = True
        elif receipt_status == "cancelled":
            seen_work.add(work_key)
            continue
        elif row["dispatch_mode"] == "launchd":
            label = "Working"
            state = "working"

        queue.append({
            "action_id": action_id,
            "item_id": row["item_id"],
            "kind": row["kind"],
            "origin": row["origin"],
            "instruction": row["instruction"],
            "note": row["note"],
            "risk": {
                "level": row["risk_level"] or "low",
                "label": row["risk_label"] or "Internal source update",
            },
            "state": state,
            "label": label,
            "needs_attention": needs_attention,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "task_title": row["task_title"] or "Free-form agent instruction",
            "category": row["category"] or "",
            "item_status": item_status or None,
            "source_state": row["source_state"] or None,
            "receipt_status": receipt_status or None,
            "summary": row["receipt_summary"] or row["instruction"] or row["note"] or "",
            "model": row["model"],
            "dispatch_mode": row["dispatch_mode"],
            "dispatch_time": row["dispatch_time"],
            "binding_authority": raw_action.get("binding_authority") or (
                "task" if row["item_id"] else "unbound"
            ),
            "binding_decision": raw_action.get("binding_decision") or (
                "attach_outcome" if row["item_id"] else "standalone"
            ),
            "binding_initial_status": raw_action.get("binding_initial_status") or (
                "open" if row["item_id"] else "missing"
            ),
            "prior_terminal_outcomes": raw_action.get("prior_terminal_outcomes") or [],
        })
        seen_work.add(work_key)
        if len(queue) >= 12:
            break

    if len(queue) < 12:
        with connect(path) as conn:
            attention_rows = conn.execute(
                """
                SELECT item_id, title, category, status, updated_at, metadata_json
                FROM work_items
                WHERE status='awaiting_human'
                ORDER BY updated_at DESC
                LIMIT 24
                """
            ).fetchall()
        for row in attention_rows:
            item_id = str(row["item_id"] or "")
            if not item_id or item_id in seen_work:
                continue
            metadata = read_metadata(row["metadata_json"])
            if metadata.get("attention_kind") != "email_reply":
                continue
            queue.append({
                "action_id": f"attention:{item_id}",
                "item_id": item_id,
                "kind": "email_reply",
                "origin": "gmail",
                "instruction": metadata.get("attention_summary") or "",
                "note": "",
                "risk": {
                    "level": "low",
                    "label": "Inbound Gmail reply",
                },
                "state": "awaiting_human",
                "label": metadata.get("attention_label") or "New reply",
                "needs_attention": True,
                "created_at": metadata.get("attention_at") or row["updated_at"],
                "updated_at": row["updated_at"],
                "task_title": row["title"] or "Gmail reply",
                "category": row["category"] or "",
                "item_status": row["status"] or None,
                "source_state": None,
                "receipt_status": "attention",
                "summary": metadata.get("attention_summary") or "New Gmail reply on this work item.",
                "model": "",
                "dispatch_mode": "attention",
                "dispatch_time": metadata.get("attention_at") or row["updated_at"],
                "attention_url": metadata.get("attention_url") or metadata.get("gmail_url") or "",
            })
            seen_work.add(item_id)
            if len(queue) >= 12:
                break
    return queue


def completed_receipt_suppresses_prior_attention(summary: str) -> bool:
    text = summary.lower()
    nonoperative_markers = (
        "non-operative",
        "no separate side effect",
        "no standalone request",
        "continuation marker",
    )
    return not any(marker in text for marker in nonoperative_markers)


def agent_queue_row_needs_attention(row: sqlite3.Row) -> bool:
    """Whether an action is an unresolved obligation that must stay visible."""
    item_status = row["item_status"] or ""
    receipt_status = row["receipt_effective_status"] or row["receipt_status"] or ""
    return (
        item_status == "awaiting_human"
        or row["action_status"] == "needs_clarification"
        or receipt_status in {"blocked", "failed", "awaiting_approval"}
    )


def get_work_item_context(path: Path, item_id: str) -> dict[str, Any] | None:
    """Return the complete queryable history for one durable work item."""
    init_db(path)
    item = get_work_item(path, item_id)
    with connect(path) as conn:
        if not item:
            row = conn.execute("SELECT * FROM work_items WHERE item_id=?", (item_id,)).fetchone()
            if not row:
                return None
            item = {
                "id": row["item_id"],
                "title": row["title"],
                "detail": row["body"],
                "cat": row["category"],
                "urgency": row["urgency"],
                "dbStatus": row["status"],
                "today": bool(row["today"]),
                "sourceState": row["source_state"],
            }
        turns = [
            {
                "turn_id": row["turn_id"],
                "action_id": row["action_id"],
                "actor_type": row["actor_type"],
                "turn_type": row["turn_type"],
                "content": row["content"],
                "created_at": row["created_at"],
                "metadata": read_metadata(row["metadata_json"]),
            }
            for row in conn.execute(
                "SELECT * FROM turns WHERE item_id=? ORDER BY created_at, turn_id",
                (item_id,),
            ).fetchall()
        ]
        artifacts = [
            {
                "artifact_id": row["artifact_id"],
                "action_id": row["action_id"],
                "turn_id": row["turn_id"],
                "artifact_type": row["artifact_type"],
                "label": row["label"],
                "content": row["content"],
                "uri": row["uri"],
                "external_id": row["external_id"],
                "status": row["status"],
                "version": row["version"],
                "supersedes_artifact_id": row["supersedes_artifact_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "metadata": read_metadata(row["metadata_json"]),
            }
            for row in conn.execute(
                "SELECT * FROM artifacts WHERE item_id=? ORDER BY created_at, artifact_id",
                (item_id,),
            ).fetchall()
        ]
        events = [dict(row) for row in conn.execute(
            "SELECT event_id, event_type, summary, status, occurred_at, source_action_id FROM work_item_events WHERE item_id=? ORDER BY occurred_at, event_id",
            (item_id,),
        ).fetchall()]
        references = [dict(row) for row in conn.execute(
            """
            SELECT s.source_id, s.source_type, s.uri, s.label, wis.source_item_id,
                   wis.raw_text, wis.first_seen_at, wis.last_seen_at
            FROM work_item_sources wis
            JOIN sources s ON s.source_id=wis.source_id
            WHERE wis.item_id=?
            ORDER BY wis.first_seen_at, s.source_id
            """,
            (item_id,),
        ).fetchall()]
        runs = [dict(row) for row in conn.execute(
            """
            SELECT DISTINCT r.run_id, r.dispatch_id, r.run_type, r.status,
                            r.started_at, r.finished_at, r.model, r.error
            FROM runs r
            JOIN run_actions ra ON ra.run_id=r.run_id
            JOIN actions a ON a.action_id=ra.action_id
            WHERE a.item_id=?
            ORDER BY r.started_at, r.run_id
            """,
            (item_id,),
        ).fetchall()]
    return {
        "item": item,
        "turns": turns,
        "runs": runs,
        "artifacts": artifacts,
        "events": events,
        "references": references,
    }
