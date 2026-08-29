"""Responses to queued CMD actions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ReadJsonlFn = Callable[[Path, int], list[dict[str, Any]]]
WriteJsonlFn = Callable[[Path, dict[str, Any]], Any]
ResultByActionIdFn = Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]]
SyncFn = Callable[[], Any]
NowFn = Callable[[], str]


def cancel_action(
    action_id: str,
    *,
    action_log: Path,
    result_log: Path,
    read_jsonl_fn: ReadJsonlFn,
    write_jsonl_fn: WriteJsonlFn,
    result_by_action_id_fn: ResultByActionIdFn,
    sync_fn: SyncFn,
    now_fn: NowFn,
) -> dict[str, Any]:
    return resolve_action(
        action_id,
        resolution="cancel",
        action_log=action_log,
        result_log=result_log,
        read_jsonl_fn=read_jsonl_fn,
        write_jsonl_fn=write_jsonl_fn,
        result_by_action_id_fn=result_by_action_id_fn,
        sync_fn=sync_fn,
        now_fn=now_fn,
    )


def resolve_action(
    action_id: str,
    *,
    resolution: str,
    action_log: Path,
    result_log: Path,
    read_jsonl_fn: ReadJsonlFn,
    write_jsonl_fn: WriteJsonlFn,
    result_by_action_id_fn: ResultByActionIdFn,
    sync_fn: SyncFn,
    now_fn: NowFn,
) -> dict[str, Any]:
    actions = read_jsonl_fn(action_log, 100000)
    action = next((row for row in actions if row.get("id") == action_id), None)
    if not action:
        return {"ok": False, "error": "action_not_found"}

    existing_result = result_by_action_id_fn(read_jsonl_fn(result_log, 100000)).get(action_id)
    if existing_result and resolution == "cancel":
        return {"ok": True, "already_resolved": True, "result": existing_result}

    summaries = {
        "cancel": (
            "Cancelled from the Command before an agent result was recorded. "
            "No source changes or external side effects should be performed for this action."
        ),
        "dismiss": (
            "Dismissed from the active CMD agent queue by the user. "
            "No further agent follow-up is requested for this action."
        ),
        "complete": (
            "Agent queue item handled by the user. "
            "Cleared this agent queue item; the parent outcome remains open unless the user marks it done."
        ),
    }
    statuses = {
        "cancel": "cancelled",
        "dismiss": "dismissed",
        "complete": "completed",
    }
    if resolution not in summaries:
        return {"ok": False, "error": "invalid_resolution"}

    result = {
        "action_id": action_id,
        "time": now_fn(),
        "status": statuses[resolution],
        "record_type": "human_resolution",
        "resolution": resolution,
        "summary": summaries[resolution],
    }
    write_jsonl_fn(result_log, result)
    sync_fn()
    return {"ok": True, "already_resolved": False, "result": result}


def approve_action(
    action_id: str,
    *,
    action_log: Path,
    result_log: Path,
    approval_log: Path,
    read_jsonl_fn: ReadJsonlFn,
    write_jsonl_fn: WriteJsonlFn,
    sync_fn: SyncFn,
    now_fn: NowFn,
) -> dict[str, Any]:
    actions = read_jsonl_fn(action_log, 100000)
    action = next((row for row in actions if row.get("id") == action_id), None)
    if not action:
        return {"ok": False, "error": "action_not_found"}
    results = read_jsonl_fn(result_log, 100000)
    prepared = next((row for row in reversed(results) if row.get("action_id") == action_id and row.get("status") == "awaiting_approval"), None)
    proposed = prepared.get("proposed_operation") if isinstance(prepared, dict) else None
    if not isinstance(proposed, dict) or not isinstance(proposed.get("payload"), dict):
        return {"ok": False, "error": "preview_not_ready"}

    execution_id = f"act-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{len(actions) + 1}"
    execution_operation = {
        **proposed,
        "execution_mode": "execute",
        "approved_from_action_id": action_id,
    }
    execution_action = {
        **action,
        "id": execution_id,
        "time": now_fn(),
        "origin": "approved_preview",
        "metadata": {
            **(action.get("metadata") or {}),
            "proposed_operation": execution_operation,
            "approved_preview_action_id": action_id,
        },
    }
    write_jsonl_fn(action_log, execution_action)
    approval = {
        "action_id": execution_id,
        "time": now_fn(),
        "approved": True,
        "summary": f"Approved exact preview from {action_id} in the Command Agent Inbox.",
    }
    write_jsonl_fn(approval_log, approval)
    write_jsonl_fn(result_log, {
        "action_id": action_id,
        "time": now_fn(),
        "status": "cancelled",
        "summary": f"Exact preview approved; execution queued as {execution_id}.",
    })
    sync_fn()
    return {"ok": True, "approval": approval, "execution_action": execution_action}
