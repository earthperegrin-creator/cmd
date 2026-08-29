"""Task attention marker responses for CMD."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import cmd_db


SyncFn = Callable[[], Any]
ReadQueueFn = Callable[[], list[dict[str, Any]]]
ReadTasksFn = Callable[[], list[dict[str, Any]]]


def clear_task_attention(
    db_path: Path,
    item_id: str,
    *,
    sync_fn: SyncFn,
    read_queue_fn: ReadQueueFn,
    read_tasks_fn: ReadTasksFn,
) -> dict[str, Any]:
    sync_fn()
    task = cmd_db.clear_work_item_attention(db_path, item_id)
    if not task:
        return {"ok": False, "error": "task_not_found"}
    return {"ok": True, "task": task, "queue": read_queue_fn(), "tasks": read_tasks_fn()}
