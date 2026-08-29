#!/usr/bin/env python3
"""Shared process lifecycle for CMD's explicit headless worker adapters."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class WorkerFiles:
    dispatch_log: Path
    result_log: Path
    heartbeat_log: Path
    last_message: Path | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def append_failure_receipts(files: WorkerFiles, dispatch_id: str, summary: str) -> int:
    """Turn a worker-process failure into an honest terminal queue state."""
    dispatch = next(
        (row for row in _read_jsonl(files.dispatch_log) if row.get("id") == dispatch_id),
        None,
    )
    if not dispatch:
        return 0

    existing = {
        row.get("action_id")
        for row in _read_jsonl(files.result_log)
        if not (
            row.get("model") == "queue-watcher"
            and str(row.get("status") or "") in {"failed", "blocked"}
            and "no background worker picked up this action"
            in str(row.get("summary") or "").lower()
        )
    }
    receipts = [
        {
            "action_id": action_id,
            "time": _utc_now(),
            "status": "failed",
            "summary": summary,
            "model": "worker-launch",
        }
        for action_id in dispatch.get("action_ids", [])
        if action_id not in existing
    ]
    if not receipts:
        return 0

    files.result_log.parent.mkdir(parents=True, exist_ok=True)
    with files.result_log.open("a", encoding="utf-8") as handle:
        for receipt in receipts:
            handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
    return len(receipts)


def append_heartbeat(files: WorkerFiles, dispatch_id: str, state: str = "working") -> None:
    files.heartbeat_log.parent.mkdir(parents=True, exist_ok=True)
    row = {"dispatch_id": dispatch_id, "time": _utc_now(), "state": state}
    with files.heartbeat_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def heartbeat_loop(
    files: WorkerFiles,
    dispatch_id: str,
    stop: threading.Event,
    interval: float = 30.0,
) -> None:
    append_heartbeat(files, dispatch_id, "started")
    while not stop.wait(interval):
        append_heartbeat(files, dispatch_id)


def run_worker(
    command: Sequence[str],
    *,
    dispatch_id: str,
    cwd: Path,
    files: WorkerFiles,
    timeout_seconds: int,
    capture_output: bool = False,
) -> int:
    """Run one adapter command with identical queue and liveness semantics."""
    stop_heartbeat = threading.Event()
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop,
        args=(files, dispatch_id, stop_heartbeat),
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
            capture_output=capture_output,
            text=capture_output,
        )
        if capture_output:
            if completed.stdout:
                sys.stdout.write(completed.stdout)
                if files.last_message is not None:
                    files.last_message.parent.mkdir(parents=True, exist_ok=True)
                    files.last_message.write_text(completed.stdout, encoding="utf-8")
            if completed.stderr:
                sys.stderr.write(completed.stderr)
        if completed.returncode != 0:
            append_failure_receipts(
                files,
                dispatch_id,
                f"Background agent exited with code {completed.returncode} before returning a result. Retry or continue in Agent mode.",
            )
        return completed.returncode
    except subprocess.TimeoutExpired:
        append_failure_receipts(
            files,
            dispatch_id,
            f"Background agent exceeded the {timeout_seconds // 60}-minute execution limit. Retry or continue in Agent mode.",
        )
        return 124
    except OSError as error:
        append_failure_receipts(files, dispatch_id, f"Background agent could not start: {error}")
        print(f"worker startup failed: {error}", file=sys.stderr)
        return 127
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=1)
        append_heartbeat(files, dispatch_id, "worker_exited")
