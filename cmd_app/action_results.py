"""Action receipt and queue-status helpers for CMD."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from cmd_runtime.google_doc_contract import (
    google_doc_contract_summary,
    result_blocks_on_google_doc_contract,
)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def result_by_action_id(
    results: list[dict[str, Any]],
    *,
    recovery_seconds: int,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    return {
        result.get("action_id"): result
        for result in results
        if result.get("action_id")
        and not result_is_recoverable_pickup_miss(result, now=now, recovery_seconds=recovery_seconds)
    }


def result_is_pickup_miss(result: dict[str, Any]) -> bool:
    status = str(result.get("status") or "")
    summary = str(result.get("summary") or "").lower()
    return (
        status in {"failed", "blocked"}
        and result.get("model") == "queue-watcher"
        and "no background worker picked up this action" in summary
    )


def result_is_recoverable_pickup_miss(
    result: dict[str, Any],
    *,
    recovery_seconds: int,
    now: datetime | None = None,
) -> bool:
    if not result_is_pickup_miss(result):
        return False
    receipt_time = parse_iso_datetime(result.get("time") or result.get("timestamp"))
    if not receipt_time:
        return False
    if receipt_time.tzinfo is None:
        receipt_time = receipt_time.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - receipt_time).total_seconds() <= recovery_seconds


def effective_result_status(
    result: dict[str, Any],
    *,
    recovery_seconds: int,
    now: datetime | None = None,
) -> str:
    if result_is_recoverable_pickup_miss(result, now=now, recovery_seconds=recovery_seconds):
        return "pending"
    status = str(result.get("status") or "")
    summary = str(result.get("summary") or "").lower()
    if result_blocks_on_google_doc_contract(result):
        return "blocked"
    artifact_markers = [
        "saved fallback gmail draft",
        "fallback gmail draft",
        "saved gmail draft",
        "gmail draft",
    ]
    if status in {"failed", "blocked"} and any(marker in summary for marker in artifact_markers):
        return "completed"
    return status


def result_with_effective_status(
    result: dict[str, Any],
    *,
    recovery_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    effective_status = effective_result_status(result, now=now, recovery_seconds=recovery_seconds)
    if effective_status == result.get("status"):
        return result
    output = {
        **result,
        "raw_status": result.get("status"),
        "status": effective_status,
    }
    if result_blocks_on_google_doc_contract(result):
        output["summary"] = google_doc_contract_summary(result, str(result.get("summary") or ""))
        output["google_doc_contract_blocked"] = True
    else:
        output["artifact_recovered"] = True
    return output


def approved_action_ids(approvals: list[dict[str, Any]]) -> set[str]:
    return {
        approval.get("action_id")
        for approval in approvals
        if approval.get("action_id") and approval.get("approved")
    }


def action_age_seconds(action: dict[str, Any], now: datetime | None = None) -> float | None:
    created = parse_iso_datetime(action.get("time"))
    if not created:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - created).total_seconds()


def action_in_undo_grace(
    action: dict[str, Any],
    *,
    undo_grace_seconds: int,
    now: datetime | None = None,
) -> bool:
    age = action_age_seconds(action, now)
    return age is not None and age < undo_grace_seconds
