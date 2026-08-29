"""Gmail triage routing behavior for CMD."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Any

import cmd_db


NowFn = Callable[[], str]
MAX_TRAINING_REVIEW_PER_GROUP = 2
MAX_TRAINING_REVIEW_ROWS = 30
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")


@dataclass(frozen=True)
class GmailScanDeps:
    """Server-owned collaborators needed by the Gmail scan orchestrator."""

    sync_cmd_db: Callable[[], Any]
    read_settings: Callable[[], dict[str, Any]]
    read_command_tasks: Callable[[], list[dict[str, Any]]]
    read_weekly_context: Callable[[], dict[str, Any]]
    scan_gmail_thread_candidates: Callable[[int, str, bool], dict[str, Any] | None]
    baseline_candidate_for_semantic_triage: Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], dict[str, Any]]
    run_llm_email_triage: Callable[[list[dict[str, Any]], str], tuple[list[dict[str, Any]], dict[str, Any]]]
    merge_semantic_email_triage: Callable[[list[dict[str, Any]], list[dict[str, Any]], str], tuple[list[dict[str, Any]], int]]
    thread_records_to_candidates: Callable[[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]], tuple[list[dict[str, Any]], dict[str, int]]]
    read_email_decision_refs: Callable[[set[str]], dict[str, dict[str, str]]]
    read_open_email_task_refs: Callable[[set[str]], list[dict[str, Any]]]
    read_email_candidates: Callable[[], list[dict[str, Any]]]
    email_candidate_from_message: Callable[[dict[str, str], list[dict[str, Any]], dict[str, Any]], dict[str, Any] | None]
    gmail_query_for_thread_scan: Callable[[str], str]
    parse_gmail_search_output: Callable[[str], list[dict[str, str]]]
    default_email_triage_model: str


def route_known_thread_email(
    db_path: Path,
    candidate: dict[str, Any],
    *,
    now_fn: NowFn,
) -> dict[str, Any] | None:
    """Route a new Gmail message on a previously accepted thread to its task."""
    provider = str(candidate.get("provider") or "gmail")
    thread_id = str(candidate.get("thread_id") or "")
    message_id = str(candidate.get("message_id") or "")
    item_id = cmd_db.active_work_item_for_email_thread(db_path, provider, thread_id, message_id)
    if not item_id:
        return None
    accepted_candidate = {
        **candidate,
        "status": "accepted",
        "item_id": item_id,
        "decided_at": now_fn(),
        "decision_reason": "matched_existing_thread",
    }
    stored = cmd_db.upsert_email_candidate(db_path, accepted_candidate)
    return cmd_db.mark_email_attention(db_path, item_id, {**accepted_candidate, **stored})


def route_semantic_continuation(
    db_path: Path,
    candidate: dict[str, Any],
    *,
    now_fn: NowFn,
) -> dict[str, Any] | None:
    """Route semantic Gmail continuations to an existing CMD task."""
    if str(candidate.get("relationship") or "") != "continuation":
        return None
    item_id = str(candidate.get("existing_item_id") or "")
    if not item_id:
        return None
    provider = str(candidate.get("provider") or "gmail")
    message_id = str(candidate.get("message_id") or "")
    if message_id:
        existing = cmd_db.get_email_candidate(db_path, cmd_db.candidate_id(provider, message_id))
        if (
            existing
            and existing.get("status") == "accepted"
            and str(existing.get("item_id") or "") == item_id
        ):
            return None
    task = cmd_db.get_work_item(db_path, item_id)
    if not task or str(task.get("dbStatus") or "") in {"cancelled", "superseded"}:
        return None
    accepted_candidate = {
        **candidate,
        "status": "accepted",
        "item_id": item_id,
        "decided_at": now_fn(),
        "decision_reason": "matched_existing_outcome",
        "reopen_existing": str(task.get("dbStatus") or "") in {"done", "dropped"},
    }
    stored = cmd_db.upsert_email_candidate(db_path, accepted_candidate)
    return cmd_db.mark_email_attention(db_path, item_id, {**accepted_candidate, **stored})


def email_record_message_id(record: dict[str, Any]) -> str:
    return str(record.get("last_msg_id") or record.get("message_id") or record.get("ID") or "")


def email_record_group_key(record: dict[str, Any]) -> str:
    sender = str(record.get("from") or record.get("sender") or record.get("From") or "")
    match = EMAIL_PATTERN.search(sender)
    if match:
        address = match.group(0).lower()
        domain = address.rsplit("@", 1)[-1]
        return domain or address
    return sender.lower().strip() or "unknown-sender"


def email_training_review_reason(record: dict[str, Any], fallback: str) -> tuple[str, str]:
    bucket = str(record.get("bucket") or "").strip()
    sender = str(record.get("from") or record.get("sender") or record.get("From") or "")
    subject = str(record.get("subject") or record.get("Subject") or "")
    snippet = str(record.get("snippet") or record.get("Snippet") or "")
    body = str(record.get("body") or "")
    labels = str(record.get("labels") or record.get("Labels") or "")
    combined = f"{sender} {subject} {snippet} {body} {labels}".lower()
    if "list-unsubscribe" in combined or "newsletter" in combined or "notification" in combined:
        return "Block pattern", "block_pattern"
    if bucket and bucket != "owes":
        return f"Training sample: CMD would normally suppress this because the thread bucket is {bucket}.", "ignore"
    return fallback, "ignore"


def upsert_training_review_samples(
    db_path: Path,
    records: list[dict[str, Any]],
    *,
    eligible_message_ids: set[str],
    proposed_message_ids: set[str],
    matched_message_ids: set[str],
    tasks: list[dict[str, Any]],
    weekly_context: dict[str, Any],
    baseline_fn: Callable[[dict[str, Any], list[dict[str, Any]], dict[str, Any]], dict[str, Any]],
    fallback_reason: str,
) -> list[dict[str, Any]]:
    """Persist a sampled stream of non-candidate Gmail rows for human training."""

    grouped_total: dict[str, int] = {}
    for record in records:
        message_id = email_record_message_id(record)
        if not message_id or message_id not in eligible_message_ids:
            continue
        if message_id in proposed_message_ids or message_id in matched_message_ids:
            continue
        group_key = email_record_group_key(record)
        grouped_total[group_key] = grouped_total.get(group_key, 0) + 1

    selected_per_group: dict[str, int] = {}
    stored: list[dict[str, Any]] = []
    for record in records:
        if len(stored) >= MAX_TRAINING_REVIEW_ROWS:
            break
        message_id = email_record_message_id(record)
        if not message_id or message_id not in eligible_message_ids:
            continue
        if message_id in proposed_message_ids or message_id in matched_message_ids:
            continue
        group_key = email_record_group_key(record)
        if selected_per_group.get(group_key, 0) >= MAX_TRAINING_REVIEW_PER_GROUP:
            continue
        selected_per_group[group_key] = selected_per_group.get(group_key, 0) + 1
        reason, suggested_label = email_training_review_reason(record, fallback_reason)
        group_count = grouped_total.get(group_key, 1)
        if group_count > 1:
            reason = f"{reason} Similar sender/domain messages in this scan: {group_count}."
        candidate = dict(baseline_fn(record, tasks, weekly_context) or {})
        if not candidate.get("message_id"):
            candidate["message_id"] = message_id
        candidate.update({
            "status": "training_review",
            "reason": reason,
            "score": min(float(candidate.get("score") or 0), 1.0),
            "suggested_training_label": suggested_label,
            "training_group_key": group_key,
            "training_group_count": group_count,
            "relationship": "none",
        })
        stored.append(cmd_db.upsert_email_candidate(db_path, candidate))
    return stored


def scan_gmail_candidates(
    db_path: Path,
    *,
    limit: int = 40,
    query: str = "newer_than:7d",
    rescan: bool = False,
    exclude_message_ids: set[str] | None = None,
    incremental: bool = False,
    now_fn: NowFn,
    deps: GmailScanDeps,
) -> dict[str, Any]:
    deps.sync_cmd_db()
    settings = deps.read_settings()
    semantic_enabled = bool(settings.get("email_triage_enabled"))
    semantic_model = str(settings.get("email_triage_model") or deps.default_email_triage_model)
    known_statuses = cmd_db.email_candidate_statuses(db_path, provider="gmail")
    tasks = deps.read_command_tasks()
    weekly_context = deps.read_weekly_context()
    proposed = []
    matched_reply_tasks = []
    matched_reply_message_ids: set[str] = set()
    skipped_known = 0
    skipped_reasons: dict[str, int] = {}
    excluded = {str(message_id) for message_id in (exclude_message_ids or set()) if message_id}
    thread_scan = None
    try:
        thread_scan = deps.scan_gmail_thread_candidates(limit, query, semantic_enabled)
    except Exception as exc:
        thread_scan = {"error": str(exc)}

    if thread_scan and not thread_scan.get("error"):
        model_usage: dict[str, Any] = {}
        records = thread_scan.get("records", [])
        if semantic_enabled:
            skipped_reasons = {
                "their_court": 0,
                "awaiting": 0,
                "auto": 0,
                "broadcast": 0,
                "calendar": 0,
                "not_actionable": 0,
                "existing_task": 0,
            }
            semantic_records = []
            exact_thread_records = []
            for record in records:
                bucket = str(record.get("bucket") or "")
                if bucket != "owes":
                    key = "calendar" if bucket == "cal_invite" else bucket
                    skipped_reasons[key] = skipped_reasons.get(key, 0) + 1
                    continue
                message_id = str(record.get("last_msg_id") or "")
                if message_id and (
                    message_id in excluded
                    or (message_id in known_statuses and not rescan)
                ):
                    skipped_known += 1
                    continue
                item_id = cmd_db.active_work_item_for_email_thread(
                    db_path,
                    "gmail",
                    str(record.get("thread_id") or ""),
                    str(record.get("last_msg_id") or ""),
                )
                if item_id:
                    exact_thread_records.append(record)
                    continue
                semantic_records.append(record)
            for record in exact_thread_records:
                candidate = deps.baseline_candidate_for_semantic_triage(record, tasks, weekly_context)
                routed_task = route_known_thread_email(db_path, candidate, now_fn=now_fn)
                if routed_task:
                    matched_reply_tasks.append(routed_task)
                    matched_reply_message_ids.add(str(record.get("last_msg_id") or ""))
            baselines = [deps.baseline_candidate_for_semantic_triage(record, tasks, weekly_context) for record in semantic_records]
            try:
                triage_rows, model_usage = deps.run_llm_email_triage(semantic_records, semantic_model)
            except Exception as exc:
                return {
                    "ok": False,
                    "error": "semantic_triage_failed",
                    "detail": str(exc),
                    "query": thread_scan.get("query", query),
                    "scanned": thread_scan.get("scanned", 0),
                    "triage_model": semantic_model,
                }
            candidates, semantic_filtered = deps.merge_semantic_email_triage(baselines, triage_rows, semantic_model)
            skipped_reasons["not_actionable"] += semantic_filtered
        else:
            rule_records = []
            for record in records:
                message_id = str(record.get("last_msg_id") or "")
                if message_id and (
                    message_id in excluded
                    or (message_id in known_statuses and not rescan)
                ):
                    skipped_known += 1
                    continue
                rule_records.append(record)
            candidates, skipped_reasons = deps.thread_records_to_candidates(rule_records, tasks, weekly_context)
        seen_message_ids = {str(record.get("last_msg_id") or "") for record in thread_scan.get("records", []) if record.get("last_msg_id")}
        new_message_ids = {
            message_id for message_id in seen_message_ids
            if message_id not in excluded
            and (rescan or message_id not in known_statuses)
        }
        proposed_message_ids = {str(candidate.get("message_id") or "") for candidate in candidates if candidate.get("message_id")}
        filtered_message_ids = new_message_ids - proposed_message_ids - matched_reply_message_ids
        training_review_rows = upsert_training_review_samples(
            db_path,
            list(records),
            eligible_message_ids=new_message_ids,
            proposed_message_ids=proposed_message_ids,
            matched_message_ids=matched_reply_message_ids,
            tasks=tasks,
            weekly_context=weekly_context,
            baseline_fn=deps.baseline_candidate_for_semantic_triage,
            fallback_reason="Training sample: CMD did not promote this as task-shaped mail.",
        )
        decision_refs = deps.read_email_decision_refs(seen_message_ids) if rescan else {}
        for candidate in candidates:
            message_id = candidate.get("message_id", "")
            if message_id:
                seen_message_ids.add(message_id)
            routed_task = route_known_thread_email(db_path, candidate, now_fn=now_fn)
            if routed_task:
                matched_reply_tasks.append(routed_task)
                matched_reply_message_ids.add(str(message_id))
                continue
            routed_task = route_semantic_continuation(db_path, candidate, now_fn=now_fn)
            if routed_task:
                matched_reply_tasks.append(routed_task)
                matched_reply_message_ids.add(str(message_id))
                continue
            decision = decision_refs.get(str(message_id)) if rescan else None
            if decision:
                candidate_status = decision.get("candidate_status", "")
                item_status = decision.get("item_status", "")
                if candidate_status in {"rejected", "done"} or (candidate_status == "accepted" and item_status != "open"):
                    candidate["revive_decided"] = True
            if message_id and message_id in known_statuses and not rescan:
                skipped_known += 1
                continue
            proposed.append(cmd_db.upsert_email_candidate(db_path, candidate))
        retired = (
            0
            if incremental
            else cmd_db.retire_unseen_email_candidates(db_path, "gmail", seen_message_ids)
        )
        filtered = cmd_db.retire_filtered_email_candidates(db_path, "gmail", filtered_message_ids)
        known_open_items = deps.read_open_email_task_refs(seen_message_ids)
        return {
            "ok": True,
            "query": thread_scan.get("query", query),
            "scanned": thread_scan.get("scanned", 0),
            "proposed": len(proposed),
            "matched_replies": len(matched_reply_tasks),
            "matched_reply_tasks": matched_reply_tasks,
            "known_open": len(known_open_items),
            "known_open_items": known_open_items,
            "skipped_known": skipped_known,
            "skipped_reasons": skipped_reasons,
            "retired": retired,
            "filtered": filtered,
            "training_review": len(training_review_rows),
            "capped": bool(thread_scan.get("capped")),
            "failed": int(thread_scan.get("failed") or 0),
            "mode": "semantic_threads" if semantic_enabled else "threads",
            "triage": model_usage,
            "observed_message_ids": sorted(seen_message_ids),
            "new_message_ids": sorted(new_message_ids),
            "candidates": deps.read_email_candidates(),
        }

    if semantic_enabled:
        return {
            "ok": False,
            "error": "gmail_thread_scan_failed",
            "detail": thread_scan.get("error") if isinstance(thread_scan, dict) else "thread scanner unavailable",
            "triage_model": semantic_model,
        }

    fallback_query = deps.gmail_query_for_thread_scan(query)
    command = ["gmail", "search", "--limit", str(max(1, min(limit, 100))), fallback_query]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
    if completed.returncode != 0:
        return {
            "ok": False,
            "error": "gmail_search_failed",
            "stderr": completed.stderr.strip(),
            "stdout": completed.stdout.strip(),
            "query": fallback_query,
            "thread_error": thread_scan.get("error") if isinstance(thread_scan, dict) else "",
        }
    messages = deps.parse_gmail_search_output(completed.stdout)
    seen_message_ids = {message.get("ID", "") for message in messages if message.get("ID")}
    new_message_ids = {
        message_id for message_id in seen_message_ids
        if message_id not in excluded
        and (rescan or message_id not in known_statuses)
    }
    proposed_message_ids: set[str] = set()
    decision_refs = deps.read_email_decision_refs(seen_message_ids) if rescan else {}
    for message in messages:
        message_id = message.get("ID", "")
        if message_id and (
            message_id in excluded
            or (message_id in known_statuses and not rescan)
        ):
            skipped_known += 1
            continue
        candidate = deps.email_candidate_from_message(message, tasks, weekly_context)
        if not candidate:
            continue
        routed_task = route_known_thread_email(db_path, candidate, now_fn=now_fn)
        if routed_task:
            matched_reply_tasks.append(routed_task)
            if message_id:
                matched_reply_message_ids.add(message_id)
            continue
        decision = decision_refs.get(str(message_id)) if rescan else None
        if decision:
            candidate_status = decision.get("candidate_status", "")
            item_status = decision.get("item_status", "")
            if candidate_status in {"rejected", "done"} or (candidate_status == "accepted" and item_status != "open"):
                candidate["revive_decided"] = True
        if message_id:
            proposed_message_ids.add(message_id)
        proposed.append(cmd_db.upsert_email_candidate(db_path, candidate))
    retired = (
        0
        if incremental
        else cmd_db.retire_unseen_email_candidates(db_path, "gmail", seen_message_ids)
    )
    filtered_message_ids = new_message_ids - proposed_message_ids - matched_reply_message_ids
    fallback_records = [
        {
            "last_msg_id": message.get("ID", ""),
            "thread_id": message.get("Thread ID", ""),
            "from": message.get("From", ""),
            "subject": message.get("Subject", ""),
            "snippet": message.get("Snippet", ""),
            "body": message.get("Snippet", ""),
            "last_date": message.get("Date", ""),
            "category": "",
            "labels": message.get("Labels", ""),
            "bucket": "unknown",
        }
        for message in messages
    ]
    training_review_rows = upsert_training_review_samples(
        db_path,
        fallback_records,
        eligible_message_ids=filtered_message_ids,
        proposed_message_ids=proposed_message_ids,
        matched_message_ids=matched_reply_message_ids,
        tasks=tasks,
        weekly_context=weekly_context,
        baseline_fn=deps.baseline_candidate_for_semantic_triage,
        fallback_reason="Training sample: CMD's fallback Gmail rules did not promote this.",
    )
    filtered = cmd_db.retire_filtered_email_candidates(db_path, "gmail", filtered_message_ids)
    known_open_items = deps.read_open_email_task_refs(seen_message_ids)
    return {
        "ok": True,
        "query": fallback_query,
        "scanned": len(messages),
        "proposed": len(proposed),
        "matched_replies": len(matched_reply_tasks),
        "matched_reply_tasks": matched_reply_tasks,
        "known_open": len(known_open_items),
        "known_open_items": known_open_items,
        "skipped_known": skipped_known,
        "skipped_reasons": skipped_reasons,
        "retired": retired,
        "filtered": filtered,
        "training_review": len(training_review_rows),
        "mode": "messages",
        "observed_message_ids": sorted(seen_message_ids),
        "new_message_ids": sorted(new_message_ids),
        "candidates": deps.read_email_candidates(),
    }
