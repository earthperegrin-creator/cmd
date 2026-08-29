"""Dispatch-log record helpers for CMD."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


ConnectorStatusFn = Callable[[dict[str, Any]], dict[str, Any]]


def dispatched_action_ids(dispatches: list[dict[str, Any]], modes: set[str] | None = None) -> set[str]:
    ids: set[str] = set()
    for dispatch in dispatches:
        if modes is not None and dispatch.get("mode") not in modes:
            continue
        for action_id in dispatch.get("action_ids", []):
            ids.add(action_id)
    return ids


def action_dispatch_modes(dispatches: list[dict[str, Any]]) -> dict[str, set[str]]:
    modes_by_action: dict[str, set[str]] = {}
    for dispatch in dispatches:
        mode = dispatch.get("mode") or "unknown"
        for action_id in dispatch.get("action_ids", []):
            modes_by_action.setdefault(action_id, set()).add(mode)
    return modes_by_action


def latest_dispatch_time_by_action(
    dispatches: list[dict[str, Any]],
    modes: set[str] | None = None,
) -> dict[str, str]:
    latest: dict[str, str] = {}
    for dispatch in dispatches:
        if modes is not None and dispatch.get("mode") not in modes:
            continue
        dispatch_time = dispatch.get("time")
        if not dispatch_time:
            continue
        for action_id in dispatch.get("action_ids", []):
            if action_id not in latest or dispatch_time > latest[action_id]:
                latest[action_id] = dispatch_time
    return latest


def latest_heartbeat_by_action(
    dispatches: list[dict[str, Any]],
    heartbeats: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    actions_by_dispatch = {
        dispatch.get("id"): dispatch.get("action_ids", [])
        for dispatch in dispatches
        if dispatch.get("id")
    }
    latest: dict[str, dict[str, Any]] = {}
    for heartbeat in heartbeats:
        heartbeat_time = heartbeat.get("time")
        for action_id in actions_by_dispatch.get(heartbeat.get("dispatch_id"), []):
            if action_id not in latest or heartbeat_time > (latest[action_id].get("time") or ""):
                latest[action_id] = heartbeat
    return latest


def queue_action_summary(
    action: dict[str, Any],
    *,
    connector_status_fn: ConnectorStatusFn,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task = action.get("task") or {}
    summary = {
        "id": action.get("id"),
        "kind": action.get("kind"),
        "time": action.get("time"),
        "task_title": task.get("title") or "Free-form agent instruction",
        "instruction": action.get("instruction") or action.get("note") or "",
        "connector_status": connector_status_fn(action),
    }
    if extra:
        summary.update(extra)
    return summary


def action_brief_line(action: dict[str, Any], index: int) -> str:
    task = action.get("task") or {}
    task_title = task.get("title") or "Free-form agent instruction"
    source = task.get("source") or "unknown source"
    instruction = action.get("instruction") or action.get("note") or ""
    return "\n".join([
        f"{index}. {action.get('kind', 'action')} / {action.get('id')}",
        f"   Task: {task_title}",
        f"   Source: {source}",
        f"   Instruction: {instruction}",
    ])
