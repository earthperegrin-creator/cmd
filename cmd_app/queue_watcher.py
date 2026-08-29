"""Local queue watcher status loop for CMD."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ReadJsonlFn = Callable[[Path, int], list[dict[str, Any]]]
ResultByActionIdFn = Callable[[list[dict[str, Any]]], dict[str, dict[str, Any]]]
ApprovedActionIdsFn = Callable[[list[dict[str, Any]] | None], set[str]]
DispatchedActionIdsFn = Callable[[list[dict[str, Any]], set[str] | None], set[str]]
LatestDispatchTimeFn = Callable[[list[dict[str, Any]], set[str] | None], dict[str, str]]
LatestHeartbeatFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, dict[str, Any]]]
ActionInUndoGraceFn = Callable[[dict[str, Any], datetime | None], bool]
ActionRequiresApprovalFn = Callable[[dict[str, Any]], bool]
ActionIsPrepareOnlyFn = Callable[[dict[str, Any]], bool]
ActionCanRunFn = Callable[[dict[str, Any], set[str]], bool]
ParseIsoDatetimeFn = Callable[[str | None], datetime | None]
AppendReceiptsFn = Callable[[list[dict[str, Any]], dict[str, str], Path | None], list[dict[str, Any]]]
EffectiveResultStatusFn = Callable[[dict[str, Any]], str]
QueueActionSummaryFn = Callable[[dict[str, Any], dict[str, Any] | None], dict[str, Any]]
ActionAgeSecondsFn = Callable[[dict[str, Any], datetime | None], float | None]
EffectiveActionRiskFn = Callable[[dict[str, Any]], dict[str, str]]
NowFn = Callable[[], str]


@dataclass(frozen=True)
class QueueWatcherDeps:
    dispatch_log: Path
    approval_log: Path
    heartbeat_log: Path
    substantive_kinds: set[str]
    dispatch_stale_seconds: int
    pickup_stale_seconds: int
    dispatch_max_runtime_seconds: int
    undo_grace_seconds: int
    read_jsonl_fn: ReadJsonlFn
    result_by_action_id_fn: ResultByActionIdFn
    approved_action_ids_fn: ApprovedActionIdsFn
    dispatched_action_ids_fn: DispatchedActionIdsFn
    latest_dispatch_time_by_action_fn: LatestDispatchTimeFn
    latest_heartbeat_by_action_fn: LatestHeartbeatFn
    action_in_undo_grace_fn: ActionInUndoGraceFn
    action_requires_approval_fn: ActionRequiresApprovalFn
    action_is_prepare_only_fn: ActionIsPrepareOnlyFn
    action_can_run_fn: ActionCanRunFn
    parse_iso_datetime_fn: ParseIsoDatetimeFn
    append_unclaimed_action_receipts_fn: AppendReceiptsFn
    append_stale_action_receipts_fn: AppendReceiptsFn
    effective_result_status_fn: EffectiveResultStatusFn
    queue_action_summary_fn: QueueActionSummaryFn
    action_age_seconds_fn: ActionAgeSecondsFn
    effective_action_risk_fn: EffectiveActionRiskFn
    now_fn: NowFn


class QueueWatcher:
    """Cheap local queue watcher.

    This watches local JSONL files and updates in-memory status. It never calls
    an agent runtime, so its token cost is zero.
    """

    def __init__(self, action_log: Path, result_log: Path, deps: QueueWatcherDeps, poll_interval: float = 1.0) -> None:
        self.action_log = action_log
        self.result_log = result_log
        self.deps = deps
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._signature: tuple[int, ...] | None = None
        self._status: dict[str, Any] = self._empty_status()

    def _empty_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "watcher": {
                "running": False,
                "poll_interval_seconds": self.poll_interval,
                "token_cost": "zero_agent_tokens",
                "behavior": "local_file_check_only",
            },
            "counts": {
                "actions": 0,
                "results": 0,
                "pending": 0,
                "undispatched": 0,
                "needs_agent_pickup": 0,
                "in_flight": 0,
                "undo_grace": 0,
                "awaiting_approval": 0,
                "ready_for_pickup": 0,
                "stale": 0,
                "blocked": 0,
                "completed": 0,
                "cancelled": 0,
                "dispatches": 0,
            },
            "stale_actions": [],
            "last_action": None,
            "last_result": None,
            "last_dispatch": None,
            "changed_at": None,
            "queue": str(self.action_log),
            "results": str(self.result_log),
            "dispatches": str(self.deps.dispatch_log),
        }

    def _file_signature(self, path: Path) -> tuple[int, int]:
        if not path.exists():
            return (0, 0)
        stat = path.stat()
        return (stat.st_mtime_ns, stat.st_size)

    def _logs_signature(self) -> tuple[int, ...]:
        return (
            *self._file_signature(self.action_log),
            *self._file_signature(self.result_log),
            *self._file_signature(self.deps.dispatch_log),
            *self._file_signature(self.deps.approval_log),
            *self._file_signature(self.deps.heartbeat_log),
        )

    def _queue_sets(
        self,
        actions: list[dict[str, Any]],
        result_by_action: dict[str, dict[str, Any]],
        approved_ids: set[str],
        sent_ids: set[str],
        launchd_sent_ids: set[str],
        now: datetime,
    ) -> dict[str, list[dict[str, Any]]]:
        pending = [
            action for action in actions
            if action.get("id") and action.get("id") not in result_by_action
        ]
        substantive_pending = [
            action for action in pending
            if action.get("kind") in self.deps.substantive_kinds
        ]
        undo_grace = [
            action for action in substantive_pending
            if self.deps.action_in_undo_grace_fn(action, now)
        ]
        awaiting_approval = [
            action for action in substantive_pending
            if not self.deps.action_in_undo_grace_fn(action, now)
            and self.deps.action_requires_approval_fn(action)
            and not self.deps.action_is_prepare_only_fn(action)
            and action.get("id") not in approved_ids
        ]
        ready_for_pickup = [
            action for action in substantive_pending
            if not self.deps.action_in_undo_grace_fn(action, now)
            and self.deps.action_can_run_fn(action, approved_ids)
        ]
        undispatched = [
            action for action in ready_for_pickup
            if action.get("id") not in sent_ids
        ]
        needs_agent_pickup = [
            action for action in ready_for_pickup
            if action.get("id") not in launchd_sent_ids
        ]
        in_flight = [
            action for action in ready_for_pickup
            if action.get("id") in launchd_sent_ids
        ]
        return {
            "pending": pending,
            "substantive_pending": substantive_pending,
            "undo_grace": undo_grace,
            "awaiting_approval": awaiting_approval,
            "ready_for_pickup": ready_for_pickup,
            "undispatched": undispatched,
            "needs_agent_pickup": needs_agent_pickup,
            "in_flight": in_flight,
        }

    def scan(self, force: bool = False) -> dict[str, Any]:
        signature = self._logs_signature()
        with self._lock:
            if not force and signature == self._signature:
                self._status["watcher"]["running"] = self.running
                return self._status

            actions = self.deps.read_jsonl_fn(self.action_log, 100000)
            results = self.deps.read_jsonl_fn(self.result_log, 100000)
            dispatches = self.deps.read_jsonl_fn(self.deps.dispatch_log, 100000)
            heartbeats = self.deps.read_jsonl_fn(self.deps.heartbeat_log, 100000)
            approvals = self.deps.read_jsonl_fn(self.deps.approval_log, 100000)
            result_by_action = self.deps.result_by_action_id_fn(results)
            approved_ids = self.deps.approved_action_ids_fn(approvals)
            sent_ids = self.deps.dispatched_action_ids_fn(dispatches, None)
            launchd_sent_ids = self.deps.dispatched_action_ids_fn(dispatches, {"launchd"})
            launchd_times = self.deps.latest_dispatch_time_by_action_fn(dispatches, {"launchd"})
            ui_queue_times = self.deps.latest_dispatch_time_by_action_fn(dispatches, {"auto", "manual"})
            heartbeat_by_action = self.deps.latest_heartbeat_by_action_fn(dispatches, heartbeats)
            now = datetime.now(timezone.utc)
            sets = self._queue_sets(actions, result_by_action, approved_ids, sent_ids, launchd_sent_ids, now)

            unclaimed = []
            for action in sets["needs_agent_pickup"]:
                queued_at = self.deps.parse_iso_datetime_fn(ui_queue_times.get(action.get("id")))
                if not queued_at:
                    continue
                if queued_at.tzinfo is None:
                    queued_at = queued_at.replace(tzinfo=timezone.utc)
                if (now - queued_at).total_seconds() >= self.deps.pickup_stale_seconds:
                    unclaimed.append(action)
            unclaimed_receipts = self.deps.append_unclaimed_action_receipts_fn(
                unclaimed, ui_queue_times, self.result_log
            )
            if unclaimed_receipts:
                results.extend(unclaimed_receipts)
                result_by_action = self.deps.result_by_action_id_fn(results)
                sets = self._queue_sets(actions, result_by_action, approved_ids, sent_ids, launchd_sent_ids, now)

            stale = []
            for action in sets["in_flight"]:
                action_id = action.get("id")
                last_signal = heartbeat_by_action.get(action_id) or {"time": launchd_times.get(action_id)}
                last_signal_at = self.deps.parse_iso_datetime_fn(last_signal.get("time"))
                dispatched_at = self.deps.parse_iso_datetime_fn(launchd_times.get(action_id))
                if not last_signal_at:
                    continue
                if last_signal_at.tzinfo is None:
                    last_signal_at = last_signal_at.replace(tzinfo=timezone.utc)
                if dispatched_at and dispatched_at.tzinfo is None:
                    dispatched_at = dispatched_at.replace(tzinfo=timezone.utc)
                heartbeat_silent = (now - last_signal_at).total_seconds() >= self.deps.dispatch_stale_seconds
                runtime_exceeded = bool(
                    dispatched_at
                    and (now - dispatched_at).total_seconds() >= self.deps.dispatch_max_runtime_seconds
                )
                if heartbeat_silent or runtime_exceeded:
                    stale.append(action)
            stale_receipts = self.deps.append_stale_action_receipts_fn(stale, launchd_times, self.result_log)
            if stale_receipts:
                results.extend(stale_receipts)
                result_by_action = self.deps.result_by_action_id_fn(results)
                sets = self._queue_sets(actions, result_by_action, approved_ids, sent_ids, launchd_sent_ids, now)
                stale = []

            blocked = [
                result for result in result_by_action.values()
                if self.deps.effective_result_status_fn(result) in {"blocked", "failed"}
            ]
            completed = [
                result for result in result_by_action.values()
                if self.deps.effective_result_status_fn(result) == "completed"
            ]
            cancelled = [
                result for result in result_by_action.values()
                if result.get("status") == "cancelled"
            ]
            changed = signature != self._signature
            self._signature = signature
            self._status = {
                "ok": True,
                "watcher": {
                    "running": self.running,
                    "poll_interval_seconds": self.poll_interval,
                    "token_cost": "zero_agent_tokens",
                    "behavior": "local_file_check_only",
                },
                "counts": {
                    "actions": len(actions),
                    "results": len(results),
                    "pending": len(sets["pending"]),
                    "undispatched": len(sets["undispatched"]),
                    "needs_agent_pickup": len(sets["needs_agent_pickup"]),
                    "in_flight": len(sets["in_flight"]),
                    "undo_grace": len(sets["undo_grace"]),
                    "awaiting_approval": len(sets["awaiting_approval"]),
                    "ready_for_pickup": len(sets["ready_for_pickup"]),
                    "stale": len(stale),
                    "blocked": len(blocked),
                    "completed": len(completed),
                    "cancelled": len(cancelled),
                    "dispatches": len(dispatches),
                },
                "stale_actions": [
                    self.deps.queue_action_summary_fn(action, {
                        "launchd_dispatch_time": launchd_times.get(action.get("id")),
                        "stale_after_seconds": self.deps.dispatch_stale_seconds,
                    })
                    for action in stale[-5:]
                ],
                "undo_grace_actions": [
                    self.deps.queue_action_summary_fn(action, {
                        "grace_remaining_seconds": max(0, int(self.deps.undo_grace_seconds - (self.deps.action_age_seconds_fn(action, now) or 0))),
                    })
                    for action in sets["undo_grace"][-5:]
                ],
                "awaiting_approval_actions": [
                    self.deps.queue_action_summary_fn(action, {
                        "risk": self.deps.effective_action_risk_fn(action),
                    })
                    for action in sets["awaiting_approval"][-5:]
                ],
                "needs_agent_pickup_actions": [
                    self.deps.queue_action_summary_fn(action, {
                        "risk": self.deps.effective_action_risk_fn(action),
                    })
                    for action in sets["needs_agent_pickup"][-6:]
                ],
                "in_flight_actions": [
                    self.deps.queue_action_summary_fn(action, {
                        "risk": self.deps.effective_action_risk_fn(action),
                        "launchd_dispatch_time": launchd_times.get(action.get("id")),
                        "last_heartbeat_time": (heartbeat_by_action.get(action.get("id")) or {}).get("time"),
                        "heartbeat_state": (heartbeat_by_action.get(action.get("id")) or {}).get("state"),
                        "max_runtime_seconds": self.deps.dispatch_max_runtime_seconds,
                    })
                    for action in sets["in_flight"][-6:]
                ],
                "last_action": actions[-1] if actions else None,
                "last_result": results[-1] if results else None,
                "last_dispatch": dispatches[-1] if dispatches else None,
                "changed_at": self.deps.now_fn() if changed or force else self._status.get("changed_at"),
                "queue": str(self.action_log),
                "results": str(self.result_log),
                "dispatches": str(self.deps.dispatch_log),
            }
            return self._status

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self.scan(force=True)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cmd-queue-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self.scan()
