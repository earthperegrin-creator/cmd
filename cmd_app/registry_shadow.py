"""Observation-only registry resolver shadowing for CMD action intake."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from cmd_runtime.resolution import ResolverRegistry
from cmd_runtime.resolution_planner import ProposalFn, resolve_registry_request


ACTIVE_OUTCOME_STATUSES = frozenset({"open", "awaiting_human", "blocked"})
SHADOW_OUTCOME_CANDIDATE_LIMIT = 12
SHADOW_OUTCOME_SCAN_LIMIT = 1_000
OUTCOME_CATEGORIES = frozenset({
    "deal", "post", "comms", "auto", "admin", "writing", "networking",
    "building", "learning", "personal", "trip",
})
OUTCOME_MATCH_STOPWORDS = frozenset({
    "a", "action", "an", "and", "as", "at", "be", "complete", "completed",
    "done", "first", "for", "from", "i", "in", "is", "it", "mark", "me",
    "my", "next", "no", "of", "on", "or", "remains", "something", "step",
    "that", "the", "this", "to", "with",
})
PROFILE_FIELDS = frozenset({
    "identity", "roles", "priorities", "working_style", "outcome_categories",
    "guardrails",
})
ROUTE_FIELDS = frozenset({
    "intent", "capability", "target", "execution_mode", "risk_level",
    "confidence", "negated", "already_done", "model",
})
LEGACY_OPERATION_EQUIVALENTS = {
    "gmail.draft": frozenset({"gmail.draft"}),
    "gmail.send": frozenset({"gmail.send"}),
    "calendar.create": frozenset({"calendar.create"}),
    "local.write": frozenset({"local.write"}),
    "web.read/source.read": frozenset({"web.search", "web.open"}),
}


class RegistryShadowError(ValueError):
    """Raised when a shadow envelope is malformed."""


def load_active_outcome_candidates(
    db_path: Path,
    *,
    limit: int = 100,
) -> tuple[dict[str, str], ...]:
    """Read compact root outcomes through a read-only SQLite connection."""
    if limit < 1 or limit > SHADOW_OUTCOME_SCAN_LIMIT:
        raise RegistryShadowError(
            f"outcome candidate limit must be between 1 and {SHADOW_OUTCOME_SCAN_LIMIT}"
        )
    if not db_path.exists():
        return ()
    uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
              root.item_id,
              root.title,
              root.body,
              root.category,
              root.status,
              (
                SELECT child.title
                FROM work_items child
                WHERE child.parent_item_id = root.item_id
                  AND child.status IN ('open', 'awaiting_human', 'blocked')
                ORDER BY child.sort_order, child.updated_at DESC, child.item_id
                LIMIT 1
              ) AS next_move
            FROM work_items root
            WHERE root.parent_item_id IS NULL
              AND root.status IN ('open', 'awaiting_human', 'blocked')
            ORDER BY
              CASE root.status
                WHEN 'awaiting_human' THEN 0
                WHEN 'blocked' THEN 1
                ELSE 2
              END,
              root.today DESC,
              COALESCE(root.last_action_at, root.last_seen_at, root.updated_at) DESC,
              root.item_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error as error:
        raise RegistryShadowError(f"active outcomes are unreadable: {error}") from error
    finally:
        if "connection" in locals():
            connection.close()
    return tuple({
        "item_id": str(row["item_id"]),
        "title": str(row["title"] or ""),
        "category": str(row["category"] or ""),
        "status": str(row["status"] or "open"),
        "summary": str(row["body"] or "")[:2_000],
        "next_move": str(row["next_move"] or "")[:1_000],
    } for row in rows)


def load_relevant_outcome_candidates(
    db_path: Path,
    action: Mapping[str, Any],
    *,
    limit: int = SHADOW_OUTCOME_CANDIDATE_LIMIT,
) -> tuple[dict[str, str], ...]:
    """Return one task binding or a small ranked set for free-form resolution."""
    if limit < 1 or limit > 100:
        raise RegistryShadowError("outcome candidate limit must be between 1 and 100")
    task = action.get("task") if isinstance(action.get("task"), Mapping) else None
    if str(action.get("kind") or "") in {"quick_action", "codex_action"} and task:
        bound = _task_bound_outcome_candidate(db_path, task)
        if bound is not None:
            return (bound,)

    candidates = load_active_outcome_candidates(db_path, limit=SHADOW_OUTCOME_SCAN_LIMIT)
    instruction = str(action.get("instruction") or action.get("note") or "").strip()
    return select_relevant_outcome_candidates(instruction, candidates, limit=limit)


def select_relevant_outcome_candidates(
    instruction: str,
    candidates: Iterable[Mapping[str, Any]],
    *,
    limit: int = SHADOW_OUTCOME_CANDIDATE_LIMIT,
) -> tuple[dict[str, str], ...]:
    """Rank an existing active-outcome snapshot without reading live state."""
    if limit < 1 or limit > 100:
        raise RegistryShadowError("outcome candidate limit must be between 1 and 100")
    compact = _compact_outcomes(candidates)
    if len(compact) <= limit:
        return compact
    ranked = sorted(
        [
            (
                _outcome_relevance_score(instruction, row),
                -index,
                row,
            )
            for index, row in enumerate(compact)
        ],
        key=lambda value: (value[0], value[1]),
        reverse=True,
    )
    return tuple(row for _, _, row in ranked[:limit])


def select_unique_named_outcome_candidate(
    instruction: str,
    candidates: Iterable[Mapping[str, Any]],
    *,
    category: str = "",
) -> dict[str, str] | None:
    """Return one strongly named active outcome, otherwise fail closed.

    This is deliberately stricter than the broad shadow ranking above. It is
    safe for production candidate hydration because weak matches and multiple
    similarly named outcomes return no candidate instead of guessing.
    """
    compact = _compact_outcomes(candidates)
    normalized_category = category.strip().casefold()
    if normalized_category:
        compact = tuple(
            row for row in compact
            if row["category"].casefold() == normalized_category
        )
    if not compact:
        return None

    instruction_words = _normalized_words(instruction)
    instruction_text = " ".join(instruction_words)
    instruction_tokens = _match_tokens(instruction_text)
    strong: list[tuple[tuple[int, int, int, float, int], dict[str, str]]] = []
    for row in compact:
        title_words = _normalized_words(row["title"])
        item_words = _normalized_words(row["item_id"])
        title_text = " ".join(title_words)
        item_text = " ".join(item_words)
        exact = int(
            bool(title_text and title_text in instruction_text)
            or bool(item_text and item_text in instruction_text)
        )
        phrase_length = _longest_common_word_run(instruction_words, title_words)
        title_tokens = _match_tokens(title_text)
        shared_tokens = len(instruction_tokens & title_tokens)
        coverage = shared_tokens / max(1, len(title_tokens))
        relevance = _outcome_relevance_score(instruction, row)
        is_strong = (
            bool(exact)
            or (phrase_length >= 4 and shared_tokens >= 2)
            or (shared_tokens >= 3 and coverage >= 0.6 and relevance >= 24)
        )
        if is_strong:
            strong.append((
                (exact, phrase_length, shared_tokens, coverage, relevance),
                row,
            ))

    if len(strong) == 1:
        return strong[0][1]
    if not strong:
        return None
    strong.sort(key=lambda value: value[0], reverse=True)
    if strong[0][0][0] == 1 and strong[1][0][0] == 0:
        return strong[0][1]
    return None


def _task_bound_outcome_candidate(
    db_path: Path,
    task: Mapping[str, Any],
) -> dict[str, str] | None:
    item_id = str(task.get("id") or task.get("item_id") or "").strip()
    title = str(task.get("title") or "").strip()
    parent_id = str(task.get("parentId") or task.get("parent_item_id") or "").strip()
    if not parent_id and item_id and db_path.exists():
        uri = f"file:{db_path.resolve()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            row = connection.execute(
                "SELECT parent_item_id FROM work_items WHERE item_id=?",
                (item_id,),
            ).fetchone()
            parent_id = str(row[0] or "").strip() if row else ""
        except sqlite3.Error as error:
            raise RegistryShadowError(f"task binding is unreadable: {error}") from error
        finally:
            if "connection" in locals():
                connection.close()
    if parent_id and db_path.exists():
        parent = next(
            (
                row for row in load_active_outcome_candidates(
                    db_path,
                    limit=SHADOW_OUTCOME_SCAN_LIMIT,
                )
                if row["item_id"] == parent_id
            ),
            None,
        )
        if parent is not None:
            return parent
    if not item_id or not title:
        return None
    status = str(task.get("dbStatus") or task.get("status") or "open").strip()
    if status not in ACTIVE_OUTCOME_STATUSES:
        status = "open"
    return {
        "item_id": item_id[:256],
        "title": title[:500],
        "category": str(task.get("category") or task.get("cat") or "").strip()[:100],
        "status": status,
        "summary": str(task.get("detail") or task.get("body") or "").strip()[:2_000],
        "next_move": "",
    }


def _outcome_relevance_score(instruction: str, outcome: Mapping[str, str]) -> int:
    normalized = re.sub(r"[^a-z0-9]+", " ", instruction.casefold()).strip()
    tokens = _match_tokens(normalized)
    category = str(outcome.get("category") or "").casefold()
    score = 0
    if category in OUTCOME_CATEGORIES and re.search(rf"\b{re.escape(category)}\b", normalized):
        score += 12
    item_id = str(outcome.get("item_id") or "").casefold()
    title = str(outcome.get("title") or "").casefold()
    if item_id and len(item_id) >= 4 and item_id in normalized:
        score += 100
    if title and len(title) >= 8 and re.sub(r"\s+", " ", title) in normalized:
        score += 100
    score += 8 * len(tokens & _match_tokens(title))
    score += 4 * len(tokens & _match_tokens(str(outcome.get("next_move") or "")))
    score += 2 * len(tokens & _match_tokens(str(outcome.get("summary") or "")))
    return score


def _match_tokens(value: str) -> set[str]:
    return {
        token for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in OUTCOME_MATCH_STOPWORDS
    }


def _normalized_words(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def _longest_common_word_run(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    longest = 0
    for left_word in left:
        current = [0] * (len(right) + 1)
        for index, right_word in enumerate(right, start=1):
            if left_word == right_word:
                current[index] = previous[index - 1] + 1
                longest = max(longest, current[index])
        previous = current
    return longest


def registry_runtime_scope(
    registry: ResolverRegistry,
    connector_health: Mapping[str, Any] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Translate connector health into conservative baseline planning authority."""
    health = connector_health if isinstance(connector_health, Mapping) else {}
    capabilities = health.get("capabilities") if isinstance(health.get("capabilities"), Mapping) else {}
    connected = {"local-files", "web"}
    granted = {"local.read", "local.write", "web.search", "web.open"}

    gmail_read = _capability_ok(capabilities, "gmail.read")
    gmail_draft = _capability_ok(capabilities, "gmail.draft")
    if gmail_read or gmail_draft:
        connected.add("gmail")
    if gmail_read:
        granted.update({"gmail.search", "gmail.read"})
    if gmail_draft:
        granted.add("gmail.draft")

    if _capability_ok(capabilities, "calendar.read"):
        connected.add("calendar")
        granted.update({"calendar.list", "calendar.get"})

    drive_capability_operations = {
        "drive.read": ("drive.list", "drive.get", "docs.get"),
        "drive.copy": ("drive.copy",),
        "drive.download": ("drive.download",),
        "drive.export": ("drive.export",),
        "drive.create": ("drive.create",),
    }
    for capability, operations in drive_capability_operations.items():
        if not _capability_ok(capabilities, capability):
            continue
        for operation in operations:
            tool = registry.tool_for_operation(operation)
            if tool is not None:
                connected.add(tool.id)
                granted.add(operation)

    known_tools = {tool.id for tool in registry.tools()}
    known_operations = {
        operation for tool in registry.tools() for operation in tool.operations
    }
    return (
        tuple(sorted(connected & known_tools)),
        tuple(sorted(granted & known_operations)),
    )


def build_registry_shadow_request(
    action: Mapping[str, Any],
    *,
    current_route: Mapping[str, Any],
    candidate_outcomes: Iterable[Mapping[str, Any]],
    connected_tool_ids: Iterable[str],
    granted_operations: Iterable[str],
    profile: Mapping[str, Any] | None = None,
    enqueued_at: str,
) -> dict[str, Any]:
    """Build a bounded diagnostic envelope without resolving or applying work."""
    if not isinstance(action, Mapping):
        raise RegistryShadowError("action must be an object")
    instruction = str(action.get("instruction") or action.get("note") or "").strip()
    if not instruction:
        raise RegistryShadowError("shadowed action requires an instruction")
    request = {
        "instruction": instruction[:20_000],
        "origin": _resolution_origin(action),
        "recent_user_turns": _recent_user_turns(action),
        "available_inputs": _available_inputs(action),
    }
    outcomes = _compact_outcomes(candidate_outcomes)
    bounded_profile = {
        key: value for key, value in dict(profile or {}).items()
        if key in PROFILE_FIELDS
    }
    route = {
        key: value for key, value in dict(current_route or {}).items()
        if key in ROUTE_FIELDS
    }
    action_id = str(action.get("id") or action.get("action_id") or "").strip()
    request_id = str(action.get("request_id") or action.get("requestId") or action_id).strip()
    identity = {
        "action_id": action_id,
        "request_id": request_id,
        "request": request,
        "candidate_outcome_ids": [row["item_id"] for row in outcomes],
    }
    shadow_id = "shadow-" + hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "schema_version": "1",
        "shadow_id": shadow_id,
        "enqueued_at": str(enqueued_at),
        "side_effect_free": True,
        "action_id": action_id,
        "request_id": request_id,
        "request": request,
        "profile": bounded_profile,
        "candidate_outcomes": list(outcomes),
        "connected_tool_ids": _unique_strings(connected_tool_ids),
        "granted_operations": _unique_strings(granted_operations),
        "current_route": route,
    }


def evaluate_registry_shadow_request(
    envelope: Mapping[str, Any],
    registry: ResolverRegistry,
    proposal_fn: ProposalFn,
    *,
    observed_at: str,
) -> dict[str, Any]:
    """Evaluate one envelope without capture, compilation, queueing, or Tool calls."""
    if not isinstance(envelope, Mapping) or envelope.get("schema_version") != "1":
        raise RegistryShadowError("unsupported registry shadow envelope")
    reset_usage = getattr(proposal_fn, "reset_usage", None)
    if callable(reset_usage):
        reset_usage()
    attempt = resolve_registry_request(
        envelope.get("request") or {},
        registry,
        proposal_fn,
        candidate_outcomes=envelope.get("candidate_outcomes") or [],
        connected_tool_ids=envelope.get("connected_tool_ids") or [],
        granted_operations=envelope.get("granted_operations") or [],
        profile=envelope.get("profile") or {},
    )
    plan = attempt.plan.to_dict()
    current_route = dict(envelope.get("current_route") or {})
    return {
        "schema_version": "1",
        "shadow_id": str(envelope.get("shadow_id") or ""),
        "action_id": str(envelope.get("action_id") or ""),
        "request_id": str(envelope.get("request_id") or ""),
        "observed_at": str(observed_at),
        "status": "allowed" if attempt.decision.allowed else "blocked",
        "side_effects": {
            "capture_applied": False,
            "jobspec_compiled": False,
            "work_queued": False,
            "tool_provider_calls": False,
        },
        "current_route": current_route,
        "comparison": _compare_route(current_route, plan),
        "proposed_plan": attempt.proposed_plan.to_dict(),
        "canonical_plan": plan,
        "denials": [denial.to_dict() for denial in attempt.decision.denials],
        "candidate_outcome_ids": list(attempt.candidate_outcome_ids),
        "candidate_skill_ids": list(attempt.candidate_skill_ids),
        "candidate_workflow_ids": list(attempt.candidate_workflow_ids),
        "usage": _proposal_usage(proposal_fn),
    }


def append_jsonl(path: Path, row: Mapping[str, Any]) -> None:
    """Append one complete JSON line under an advisory process lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(rendered)
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RegistryShadowError(
                f"invalid JSON in {path} line {line_number}: {error}"
            ) from error
        if not isinstance(value, dict):
            raise RegistryShadowError(f"{path} line {line_number} must be an object")
        rows.append(value)
    return tuple(rows)


def pending_shadow_requests(
    queue_path: Path,
    results_path: Path,
    *,
    retry_errors: bool = False,
) -> tuple[dict[str, Any], ...]:
    completed = {
        str(row.get("shadow_id") or "")
        for row in read_jsonl(results_path)
        if not retry_errors or row.get("status") != "error"
    }
    pending: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in read_jsonl(queue_path):
        shadow_id = str(row.get("shadow_id") or "")
        if shadow_id and shadow_id not in completed and shadow_id not in seen:
            pending.append(row)
            seen.add(shadow_id)
    return tuple(pending)


def summarize_shadow_observations(
    queue_path: Path,
    results_path: Path,
    *,
    target: int = 25,
    reviews_path: Path | None = None,
) -> dict[str, Any]:
    """Summarize observation progress without resolving or mutating work."""
    if target < 1:
        raise RegistryShadowError("shadow collection target must be positive")
    queued_rows = tuple(
        row for row in read_jsonl(queue_path)
        if str(row.get("shadow_id") or "")
    )
    latest_envelopes: dict[str, dict[str, Any]] = {}
    for row in queued_rows:
        observation_id = str(
            row.get("action_id") or row.get("request_id") or row.get("shadow_id") or ""
        )
        if observation_id:
            latest_envelopes[observation_id] = row
    latest_results: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(results_path):
        shadow_id = str(row.get("shadow_id") or "")
        if shadow_id:
            latest_results[shadow_id] = row

    observations: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {}
    for observation_id, envelope in latest_envelopes.items():
        shadow_id = str(envelope.get("shadow_id") or "")
        observations[observation_id] = (envelope, latest_results.get(shadow_id))
    decisions = {
        observation_id: result
        for observation_id, (_, result) in observations.items()
        if result is not None and result.get("status") in {"allowed", "blocked"}
    }
    latest_reviews: dict[str, dict[str, Any]] = {}
    if reviews_path is not None:
        for row in read_jsonl(reviews_path):
            action_id = str(row.get("action_id") or "")
            if action_id:
                latest_reviews[action_id] = row
    review_verdicts = {
        "correct": 0,
        "acceptable_safe_clarification": 0,
        "incorrect": 0,
    }
    reviewed_action_ids: set[str] = set()
    stale_review_total = 0
    for observation_id, result in decisions.items():
        review = latest_reviews.get(observation_id)
        if review is None:
            continue
        envelope = latest_envelopes[observation_id]
        matches_latest_result = (
            str(review.get("shadow_id") or "") == str(envelope.get("shadow_id") or "")
            and str(review.get("result_observed_at") or "") == str(result.get("observed_at") or "")
        )
        verdict = str(review.get("verdict") or "")
        if not matches_latest_result or verdict not in review_verdicts:
            stale_review_total += 1
            continue
        reviewed_action_ids.add(observation_id)
        review_verdicts[verdict] += 1
    errors = {
        observation_id: result
        for observation_id, (_, result) in observations.items()
        if result is not None and result.get("status") == "error"
    }
    pending_ids = sorted(
        str(envelope.get("shadow_id") or "")
        for envelope, result in observations.values()
        if result is None
    )
    aligned = sum(
        row.get("comparison", {}).get("classification") == "aligned"
        for row in decisions.values()
    )
    different_rows = [
        _shadow_difference(latest_envelopes[observation_id], row)
        for observation_id, row in decisions.items()
        if row.get("comparison", {}).get("classification") == "different"
    ]
    side_effect_violations = sum(
        any(bool(value) for value in (row.get("side_effects") or {}).values())
        for _, row in observations.values()
        if row is not None
    )
    decision_total = len(decisions)
    reviewed_total = len(reviewed_action_ids)
    return {
        "schema_version": "1",
        "collection_target": target,
        "collection_target_met": decision_total >= target,
        "remaining_decisions": max(0, target - decision_total),
        "reviewed_total": reviewed_total,
        "review_remaining": max(0, target - reviewed_total),
        "review_target_met": reviewed_total >= target,
        "review_verdicts": review_verdicts,
        "stale_review_total": stale_review_total,
        "unreviewed_action_ids": sorted(set(decisions) - reviewed_action_ids),
        "queued_total": len(latest_envelopes),
        "envelope_total": len(queued_rows),
        "decision_total": decision_total,
        "pending_total": len(pending_ids),
        "error_total": len(errors),
        "aligned_total": aligned,
        "different_total": len(different_rows),
        "allowed_total": sum(row.get("status") == "allowed" for row in decisions.values()),
        "blocked_total": sum(row.get("status") == "blocked" for row in decisions.values()),
        "side_effect_violation_total": side_effect_violations,
        "pending_shadow_ids": pending_ids,
        "differences": different_rows,
        "errors": [
            {
                "shadow_id": str(latest_envelopes[observation_id].get("shadow_id") or ""),
                "action_id": str(row.get("action_id") or ""),
                "error": str(row.get("error") or "")[-1_200:],
            }
            for observation_id, row in errors.items()
        ],
    }


def _shadow_difference(
    envelope: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    request = envelope.get("request") if isinstance(envelope.get("request"), Mapping) else {}
    comparison = result.get("comparison") if isinstance(result.get("comparison"), Mapping) else {}
    plan = result.get("canonical_plan") if isinstance(result.get("canonical_plan"), Mapping) else {}
    outcome = plan.get("outcome") if isinstance(plan.get("outcome"), Mapping) else {}
    execution = plan.get("execution") if isinstance(plan.get("execution"), Mapping) else {}
    return {
        "shadow_id": str(result.get("shadow_id") or ""),
        "action_id": str(result.get("action_id") or ""),
        "instruction": str(request.get("instruction") or "")[:2_000],
        "current_capability": str(comparison.get("current_capability") or ""),
        "registry_admission": str(comparison.get("registry_admission") or ""),
        "outcome_id": str(outcome.get("item_id") or ""),
        "workflow_id": str(execution.get("workflow_id") or ""),
        "skill_ids": list(execution.get("skill_ids") or []),
    }


def _compact_outcomes(values: Iterable[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    if isinstance(values, (str, bytes, Mapping)):
        raise RegistryShadowError("candidate outcomes must be an array")
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        if len(results) >= 100:
            break
        if not isinstance(value, Mapping):
            continue
        item_id = str(value.get("item_id") or value.get("id") or "").strip()
        title = str(value.get("title") or "").strip()
        status = str(value.get("status") or value.get("dbStatus") or "").strip()
        if not item_id or not title or status not in ACTIVE_OUTCOME_STATUSES or item_id in seen:
            continue
        seen.add(item_id)
        subtasks = value.get("subtasks") if isinstance(value.get("subtasks"), list) else []
        next_move = str(value.get("next_move") or "").strip()
        if not next_move:
            next_move = next((
                str(row.get("title") or "").strip()
                for row in subtasks
                if isinstance(row, Mapping)
                and str(row.get("dbStatus") or row.get("status") or "") in ACTIVE_OUTCOME_STATUSES
            ), "")
        results.append({
            "item_id": item_id[:256],
            "title": title[:500],
            "category": str(value.get("category") or value.get("cat") or "").strip()[:100],
            "status": status,
            "summary": str(
                value.get("summary") or value.get("detail") or value.get("body") or ""
            ).strip()[:2_000],
            "next_move": next_move[:1_000],
        })
    return tuple(results)


def _resolution_origin(action: Mapping[str, Any]) -> str:
    kind = str(action.get("kind") or "")
    if kind == "agent_chat":
        return "agent_chat"
    if kind == "quick_action":
        return "task_card"
    if kind == "codex_action":
        return "dedicated_surface"
    return "automation"


def _recent_user_turns(action: Mapping[str, Any]) -> list[str]:
    candidates = action.get("recent_user_turns")
    if not isinstance(candidates, list):
        metadata = action.get("metadata") if isinstance(action.get("metadata"), Mapping) else {}
        candidates = metadata.get("recent_user_turns") if isinstance(metadata.get("recent_user_turns"), list) else []
    results = []
    for value in candidates[-3:]:
        text = value.get("content") if isinstance(value, Mapping) else value
        text = str(text or "").strip()
        if text:
            results.append(text[:4_000])
    return results


def _available_inputs(action: Mapping[str, Any]) -> list[str]:
    values = action.get("available_inputs")
    if not isinstance(values, list):
        metadata = action.get("metadata") if isinstance(action.get("metadata"), Mapping) else {}
        values = metadata.get("available_inputs") if isinstance(metadata.get("available_inputs"), list) else []
    return list(dict.fromkeys(
        str(value).strip()[:256] for value in values[:100] if str(value).strip()
    ))


def _capability_ok(capabilities: Mapping[str, Any], name: str) -> bool:
    value = capabilities.get(name)
    return isinstance(value, Mapping) and value.get("ok") is True


def _unique_strings(values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes, Mapping)):
        raise RegistryShadowError("runtime scope must be an array")
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _compare_route(current_route: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    capability = str(current_route.get("capability") or "none")
    admission = str(plan.get("admission") or "")
    execution = plan.get("execution") if isinstance(plan.get("execution"), Mapping) else {}
    operations = set(execution.get("tool_operations") or [])
    if capability in {"task.create", "task.update"}:
        aligned = admission == "capture_only"
    elif capability == "none":
        aligned = admission in {"no_capture", "clarify"}
    else:
        aligned = bool(LEGACY_OPERATION_EQUIVALENTS.get(capability, frozenset()) & operations)
    return {
        "classification": "aligned" if aligned else "different",
        "current_capability": capability,
        "registry_admission": admission,
        "registry_tool_operations": sorted(operations),
    }


def _proposal_usage(proposal_fn: Callable[..., Any]) -> dict[str, Any]:
    value = getattr(proposal_fn, "last_usage", {})
    return dict(value) if isinstance(value, Mapping) else {}
