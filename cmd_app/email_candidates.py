"""Email candidate lifecycle responses for CMD."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import cmd_db


SyncFn = Callable[[], Any]
ReadCandidatesFn = Callable[[], list[dict[str, Any]]]


def accept_email_candidate(
    db_path: Path,
    candidate_id: str,
    *,
    sync_fn: SyncFn,
    read_candidates_fn: ReadCandidatesFn,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sync_fn()
    task = cmd_db.accept_email_candidate(db_path, candidate_id, overrides=overrides or {})
    if not task:
        return {"ok": False, "error": "candidate_not_found"}
    return {"ok": True, "task": task, "candidates": read_candidates_fn()}


def reject_email_candidate(
    db_path: Path,
    candidate_id: str,
    *,
    sync_fn: SyncFn,
    read_candidates_fn: ReadCandidatesFn,
) -> dict[str, Any]:
    sync_fn()
    candidate = cmd_db.reject_email_candidate(db_path, candidate_id)
    if not candidate:
        return {"ok": False, "error": "candidate_not_found"}
    return {"ok": True, "candidate": candidate, "candidates": read_candidates_fn()}


def done_email_candidate(
    db_path: Path,
    candidate_id: str,
    *,
    sync_fn: SyncFn,
    read_candidates_fn: ReadCandidatesFn,
) -> dict[str, Any]:
    sync_fn()
    candidate = cmd_db.done_email_candidate(db_path, candidate_id)
    if not candidate:
        return {"ok": False, "error": "candidate_not_found"}
    return {"ok": True, "candidate": candidate, "candidates": read_candidates_fn()}


def label_email_candidate(
    db_path: Path,
    candidate_id: str,
    *,
    sync_fn: SyncFn,
    read_candidates_fn: ReadCandidatesFn,
    payload: dict[str, Any],
) -> dict[str, Any]:
    sync_fn()
    try:
        result = cmd_db.label_email_candidate(db_path, candidate_id, payload)
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    if not result:
        return {"ok": False, "error": "candidate_not_found"}
    return {"ok": True, **result, "candidates": read_candidates_fn()}
