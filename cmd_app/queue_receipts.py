"""Queue watcher receipt helpers for CMD."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


NowFn = Callable[[], str]
WriteJsonlFn = Callable[[Path, dict[str, Any]], Any]


def append_stale_action_receipts(
    stale_actions: list[dict[str, Any]],
    launchd_times: dict[str, str],
    result_log: Path,
    *,
    now_fn: NowFn,
    write_jsonl_fn: WriteJsonlFn,
) -> list[dict[str, Any]]:
    if not stale_actions:
        return []
    now = now_fn()
    receipts = []
    for action in stale_actions:
        action_id = action.get("id")
        if not action_id:
            continue
        receipts.append({
            "action_id": action_id,
            "time": now,
            "status": "failed",
            "summary": (
                "Background worker stopped reporting progress and returned no result; "
                "auto-failed so the queue does not remain stuck. "
                f"Launchd dispatch time: {launchd_times.get(action_id) or 'unknown'}."
            ),
            "model": "queue-watcher",
        })
    for receipt in receipts:
        write_jsonl_fn(result_log, receipt)
    return receipts


def append_unclaimed_action_receipts(
    actions: list[dict[str, Any]],
    queued_times: dict[str, str],
    result_log: Path,
    *,
    now_fn: NowFn,
    write_jsonl_fn: WriteJsonlFn,
) -> list[dict[str, Any]]:
    """Keep UI-queued actions open until the launchd queue tick claims them."""
    if not actions:
        return []
    return []
