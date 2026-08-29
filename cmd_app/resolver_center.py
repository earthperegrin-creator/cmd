"""Private, evaluation-only views over CMD resolver shadow cohorts."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cmd_app.registry_shadow import append_jsonl, read_jsonl


VALID_VERDICTS = {"correct", "acceptable_safe_clarification", "incorrect"}


class ResolverCenterError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_cohort(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResolverCenterError(f"resolver cohort is unreadable: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") != "1":
        raise ResolverCenterError("resolver cohort must be a schema v1 object")
    target = value.get("target")
    baseline_ids = value.get("baseline_action_ids")
    if not isinstance(target, int) or target < 1 or target > 500:
        raise ResolverCenterError("resolver cohort target is invalid")
    if not isinstance(baseline_ids, list) or any(not isinstance(row, str) for row in baseline_ids):
        raise ResolverCenterError("resolver cohort baseline_action_ids must be text")
    return value


def start_cohort(
    queue_path: Path,
    cohort_path: Path,
    *,
    cohort_id: str,
    label: str,
    target: int = 25,
    start_number: int = 1,
    resolver_checkpoint: str = "",
    model: str = "",
    reasoning_effort: str = "",
) -> dict[str, Any]:
    if cohort_path.exists():
        raise ResolverCenterError("an active resolver cohort already exists")
    if not cohort_id.strip() or not label.strip():
        raise ResolverCenterError("cohort ID and label are required")
    if target < 1 or target > 500 or start_number < 1:
        raise ResolverCenterError("cohort bounds are invalid")
    baseline_ids = sorted({
        _observation_id(row)
        for row in read_jsonl(queue_path)
        if _observation_id(row)
    })
    cohort = {
        "schema_version": "1",
        "cohort_id": cohort_id.strip()[:100],
        "label": label.strip()[:200],
        "status": "collecting",
        "target": target,
        "start_number": start_number,
        "started_at": utc_now(),
        "resolver_checkpoint": resolver_checkpoint.strip()[:100],
        "model": model.strip()[:100],
        "reasoning_effort": reasoning_effort.strip()[:50],
        "baseline_action_ids": baseline_ids,
    }
    _write_json_atomic(cohort_path, cohort)
    return cohort


def resolver_center_payload(
    queue_path: Path,
    results_path: Path,
    reviews_path: Path,
    cohort_path: Path,
) -> dict[str, Any]:
    cohort = load_cohort(cohort_path)
    if cohort is None:
        return {
            "ok": True,
            "active": False,
            "cohort": None,
            "summary": _empty_summary(),
            "cases": [],
        }

    baseline_ids = set(cohort["baseline_action_ids"])
    latest_envelopes: dict[str, dict[str, Any]] = {}
    queue_order: dict[str, int] = {}
    first_enqueued_at: dict[str, str] = {}
    for position, row in enumerate(read_jsonl(queue_path)):
        observation_id = _observation_id(row)
        if not observation_id or observation_id in baseline_ids:
            continue
        latest_envelopes[observation_id] = row
        queue_order.setdefault(observation_id, position)
        first_enqueued_at.setdefault(observation_id, str(row.get("enqueued_at") or ""))
    ordered = sorted(
        latest_envelopes.items(),
        key=lambda item: (
            first_enqueued_at[item[0]],
            queue_order[item[0]],
            item[0],
        ),
    )
    target = int(cohort["target"])
    selected = ordered[:target]
    overflow_total = max(0, len(ordered) - target)

    latest_results = {
        str(row.get("shadow_id") or ""): row
        for row in read_jsonl(results_path)
        if str(row.get("shadow_id") or "")
    }
    latest_reviews = {
        str(row.get("action_id") or ""): row
        for row in read_jsonl(reviews_path)
        if str(row.get("action_id") or "")
    }

    cases = [
        _case_payload(
            action_id,
            envelope,
            latest_results.get(str(envelope.get("shadow_id") or "")),
            latest_reviews.get(action_id),
            sequence=int(cohort.get("start_number") or 1) + index,
        )
        for index, (action_id, envelope) in enumerate(selected)
    ]
    summary = _summarize_cases(cases, target=target, overflow_total=overflow_total)
    return {
        "ok": True,
        "active": True,
        "cohort": {
            key: value
            for key, value in cohort.items()
            if key != "baseline_action_ids"
        },
        "summary": summary,
        "cases": cases,
    }


def record_review(
    queue_path: Path,
    results_path: Path,
    reviews_path: Path,
    cohort_path: Path,
    *,
    action_id: str,
    verdict: str,
    rationale: str = "",
    reviewer: str = "human",
) -> dict[str, Any]:
    verdict = verdict.strip()
    rationale = rationale.strip()
    if verdict not in VALID_VERDICTS:
        raise ResolverCenterError("resolver review verdict is invalid")
    if verdict != "correct" and not rationale:
        raise ResolverCenterError("safe clarification and incorrect reviews require a rationale")
    if len(rationale) > 2_000:
        raise ResolverCenterError("resolver review rationale is too long")
    payload = resolver_center_payload(queue_path, results_path, reviews_path, cohort_path)
    case = next((row for row in payload["cases"] if row["action_id"] == action_id), None)
    if case is None:
        raise ResolverCenterError("action is not part of the active resolver cohort")
    if case["result_status"] not in {"allowed", "blocked"}:
        raise ResolverCenterError("only completed resolver decisions can be reviewed")
    review = {
        "schema_version": "1",
        "cohort_id": str(payload["cohort"].get("cohort_id") or ""),
        "action_id": action_id,
        "shadow_id": case["shadow_id"],
        "result_observed_at": case["result_observed_at"],
        "reviewed_at": utc_now(),
        "reviewer": reviewer.strip()[:100] or "human",
        "verdict": verdict,
        "rationale": rationale,
        "correction_ids": [],
    }
    append_jsonl(reviews_path, review)
    return review


def _case_payload(
    action_id: str,
    envelope: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    review: Mapping[str, Any] | None,
    *,
    sequence: int,
) -> dict[str, Any]:
    request = envelope.get("request") if isinstance(envelope.get("request"), Mapping) else {}
    plan = result.get("canonical_plan") if result and isinstance(result.get("canonical_plan"), Mapping) else {}
    outcome = plan.get("outcome") if isinstance(plan.get("outcome"), Mapping) else {}
    execution = plan.get("execution") if isinstance(plan.get("execution"), Mapping) else {}
    side_effects = result.get("side_effects") if result and isinstance(result.get("side_effects"), Mapping) else {}
    production_capture = (
        envelope.get("production_capture")
        if isinstance(envelope.get("production_capture"), Mapping)
        else {}
    )
    shadow_id = str(envelope.get("shadow_id") or "")
    result_observed_at = str(result.get("observed_at") or "") if result else ""
    exact_review = None
    review_stale = False
    if review is not None:
        review_stale = not (
            str(review.get("shadow_id") or "") == shadow_id
            and str(review.get("result_observed_at") or "") == result_observed_at
            and str(review.get("verdict") or "") in VALID_VERDICTS
        )
        if not review_stale:
            exact_review = {
                "verdict": str(review.get("verdict") or ""),
                "rationale": str(review.get("rationale") or ""),
                "reviewed_at": str(review.get("reviewed_at") or ""),
                "reviewer": str(review.get("reviewer") or ""),
            }
    return {
        "sequence": sequence,
        "action_id": action_id,
        "shadow_id": shadow_id,
        "instruction": str(request.get("instruction") or "")[:8_000],
        "origin": str(request.get("origin") or ""),
        "source_entrypoint": str(envelope.get("source_entrypoint") or "")[:100],
        "production_capture": dict(production_capture),
        "enqueued_at": str(envelope.get("enqueued_at") or ""),
        "result_status": str(result.get("status") or "pending") if result else "pending",
        "result_observed_at": result_observed_at,
        "admission": str(plan.get("admission") or "") if result else "",
        "outcome": {
            "operation": str(outcome.get("operation") or ""),
            "item_id": str(outcome.get("item_id") or ""),
            "title": str(outcome.get("title") or ""),
        },
        "workflow_id": str(execution.get("workflow_id") or ""),
        "skill_ids": [str(value) for value in execution.get("skill_ids") or []],
        "tool_operations": [str(value) for value in execution.get("tool_operations") or []],
        "missing_inputs": [str(value) for value in plan.get("missing_inputs") or []],
        "approval_gates": [str(value) for value in plan.get("approval_gates") or []],
        "reason": str(plan.get("reason") or ""),
        "error": str(result.get("error") or "")[-1_200:] if result else "",
        "side_effect_violation": any(bool(value) for value in side_effects.values()),
        "review": exact_review,
        "review_stale": review_stale,
    }


def _summarize_cases(cases: list[dict[str, Any]], *, target: int, overflow_total: int) -> dict[str, Any]:
    decisions = [row for row in cases if row["result_status"] in {"allowed", "blocked"}]
    reviews = [row["review"] for row in cases if row["review"] is not None]
    verdicts = {verdict: 0 for verdict in sorted(VALID_VERDICTS)}
    for review in reviews:
        verdicts[review["verdict"]] += 1
    error_total = sum(row["result_status"] == "error" for row in cases)
    side_effect_total = sum(row["side_effect_violation"] for row in cases)
    collected_total = len(cases)
    decision_total = len(decisions)
    reviewed_total = len(reviews)
    if collected_total < target:
        phase = "collecting"
    elif decision_total < target:
        phase = "resolving"
    elif reviewed_total < target:
        phase = "reviewing"
    elif verdicts["incorrect"] or side_effect_total:
        phase = "failed"
    else:
        phase = "ready"
    return {
        "target": target,
        "phase": phase,
        "collected_total": collected_total,
        "collection_remaining": max(0, target - collected_total),
        "decision_total": decision_total,
        "decision_remaining": max(0, target - decision_total),
        "reviewed_total": reviewed_total,
        "review_remaining": max(0, target - reviewed_total),
        "pending_total": sum(row["result_status"] == "pending" for row in cases),
        "error_total": error_total,
        "side_effect_violation_total": side_effect_total,
        "stale_review_total": sum(row["review_stale"] for row in cases),
        "review_verdicts": verdicts,
        "overflow_total": overflow_total,
        "cutover_ready": phase == "ready",
    }


def _empty_summary() -> dict[str, Any]:
    return _summarize_cases([], target=25, overflow_total=0)


def _observation_id(row: Mapping[str, Any]) -> str:
    return str(row.get("action_id") or row.get("request_id") or row.get("shadow_id") or "")


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
