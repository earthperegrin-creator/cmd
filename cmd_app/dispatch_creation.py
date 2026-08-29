"""Create CMD dispatch records and dispatch briefs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cmd_app import dispatch_records


ReadJsonlFn = Callable[[Path, int], list[dict[str, Any]]]
WriteJsonlFn = Callable[[Path, dict[str, Any]], Any]
ReadyForPickupFn = Callable[[], list[dict[str, Any]]]
ReadSettingsFn = Callable[[], dict[str, Any]]
NowFn = Callable[[], str]


def create_dispatch(
    payload: dict[str, Any] | None = None,
    *,
    action_log: Path,
    result_log: Path,
    dispatch_log: Path,
    latest_dispatch: Path,
    read_jsonl_fn: ReadJsonlFn,
    write_jsonl_fn: WriteJsonlFn,
    ready_for_pickup_fn: ReadyForPickupFn,
    read_settings_fn: ReadSettingsFn,
    now_fn: NowFn,
) -> dict[str, Any]:
    payload = payload or {}
    mode = payload.get("mode") or "manual"
    requested_ids = set(payload.get("action_ids") or [])
    sent_modes = {"launchd"} if mode == "launchd" else {"auto", "manual"}
    already_sent = dispatch_records.dispatched_action_ids(
        read_jsonl_fn(dispatch_log, 100000),
        modes=sent_modes,
    )
    if requested_ids:
        actions = [
            action for action in ready_for_pickup_fn()
            if action.get("id") in requested_ids
            and (mode == "recovery" or action.get("id") not in already_sent)
        ]
    else:
        actions = [
            action for action in ready_for_pickup_fn()
            if mode == "recovery" or action.get("id") not in already_sent
        ]
    dispatch = {
        "id": f"dispatch-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{len(read_jsonl_fn(dispatch_log, 100000)) + 1}",
        "time": now_fn(),
        "mode": mode,
        "status": "ready_for_agent",
        "action_ids": [action["id"] for action in actions],
        "action_count": len(actions),
        "queue": str(action_log),
        "results": str(result_log),
        "brief": str(latest_dispatch),
        "note": payload.get("note") or "",
    }
    if actions:
        settings = read_settings_fn()
        brief = [
            "# Command Dispatch",
            "",
            f"Dispatch: {dispatch['id']}",
            f"Created: {dispatch['time']}",
            f"Mode: {mode}",
            f"Autonomy policy: {settings.get('autonomy_policy') or 'balanced'}",
            "",
            "Please process these queued command actions in order. For each action, perform the needed source-of-truth work, then append one result row to `.cmd/results.jsonl` with `action_id`, `status` (`completed`, `blocked`, `failed`, or `cancelled`), and `summary`.",
            "",
            "Do not mark an action completed unless the canonical source or requested artifact is actually updated. Safe autonomous work includes local source edits, public research, summaries, and reviewable drafts. Require user confirmation before third-party-visible, destructive, financial, or otherwise consequential external effects. Approval authorizes only the exact displayed payload.",
            "",
            "## Actions",
            "",
            "\n\n".join(dispatch_records.action_brief_line(action, index + 1) for index, action in enumerate(actions)),
            "",
        ]
        latest_dispatch.write_text("\n".join(brief), encoding="utf-8")
    write_jsonl_fn(dispatch_log, dispatch)
    return dispatch
