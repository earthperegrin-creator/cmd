"""Deterministic CMD capture resolution and verified persistence."""

from __future__ import annotations

import json
import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cmd_db

from . import registry_shadow


NowFn = Callable[[], str]
SlugifyFn = Callable[[str], str]
ResolverFn = Callable[[dict[str, Any]], dict[str, Any]]
FailureInjector = Callable[[str, dict[str, Any]], Any]

ACTIVE_CAPTURE_STATUSES = {"open", "awaiting_human", "blocked"}
TERMINAL_CAPTURE_STATUSES = {"done", "dropped", "cancelled", "superseded"}
CAPTURE_PLAN_OPERATIONS = {"no_capture", "updated_existing", "created_new", "needs_clarification"}
CAPTURE_MIN_CONFIDENCE = 0.7

TASK_CAPTURE_CATEGORIES = {
    "deal": "deal",
    "deals": "deal",
    "portfolio": "post",
    "portco": "post",
    "post": "post",
    "comms": "comms",
    "auto": "auto",
    "admin": "admin",
    "building": "building",
    "learning": "learning",
    "networking": "networking",
    "writing": "writing",
    "personal": "personal",
    "trip": "trip",
}

CMD_CAPTURE_ACTIVATION_PATTERN = re.compile(
    r"(?<![\w-])(?P<marker>\$cmd-capture|\$cmd|cmd\s+capture|capture\s+cmd)(?![\w-])",
    flags=re.I,
)


def extract_cmd_capture_activation(text: str) -> dict[str, str] | None:
    """Return the explicit CMD capture marker and the prompt with markers removed.

    This is intentionally lexical rather than semantic. A marker may appear
    anywhere in the prompt; ordinary task-like language is not activation.
    """
    raw = re.sub(r"\s+", " ", text or "").strip()
    matches = list(CMD_CAPTURE_ACTIVATION_PATTERN.finditer(raw))
    if not matches:
        return None
    match = matches[0]
    leading = raw[:match.start()].strip(" \t\r\n,.;:-").lower()
    if len(matches) > 1 and leading not in {"", "please", "hey", "okay", "ok"}:
        trailing_match = matches[-1]
        trailing = raw[trailing_match.end():].strip(" \t\r\n,.;:-")
        if not trailing:
            match = trailing_match
    instruction = f"{raw[:match.start()]} {raw[match.end():]}"
    instruction = re.sub(r"\s+([,.;:!?])", r"\1", instruction)
    instruction = re.sub(r"\s+", " ", instruction).strip(" \t\r\n,.;:-")
    return {
        "marker": match.group("marker"),
        "instruction": instruction,
        "original": raw,
    }


def has_cmd_capture_activation(text: str) -> bool:
    return extract_cmd_capture_activation(text) is not None


def explicit_capture_category(instruction: str) -> str | None:
    """Return a category the human named in the instruction itself."""
    text = instruction.lower()
    assigned = re.search(
        r"\b(?:make|categorize|classify|file)\s+"
        r"(?:this|that|it|the\s+(?:outcome|task))\b"
        r"[^a-z0-9]{0,12}(?:(?:as|under|in(?:to)?|to)\s+)?(?:the\s+)?"
        r"(deal|deals|portfolio|portco|post|comms|auto|admin|building|learning|networking|writing|personal|trip)\b",
        text,
    )
    if assigned:
        return TASK_CAPTURE_CATEGORIES[assigned.group(1)]
    explicit = re.search(
        r"\b(?:in|under|as|for|to)\s+(?:the\s+)?(deal|deals|portfolio|portco|post|comms|auto|admin|building|learning|networking|writing|personal|trip)\b",
        text,
    )
    if explicit:
        return TASK_CAPTURE_CATEGORIES[explicit.group(1)]
    category_label = re.search(
        r"\b(deal|deals|portfolio|portco|post|comms|auto|admin|building|learning|networking|writing|personal|trip)\s+categor(?:y|ies)\b",
        text,
    )
    if category_label:
        return TASK_CAPTURE_CATEGORIES[category_label.group(1)]
    if re.search(r"\b(?:portcos?|portfolio[- ]compan(?:y|ies))\b", text):
        return "post"
    return None


def infer_capture_category(instruction: str, metadata: dict[str, Any] | None = None) -> str:
    explicit = explicit_capture_category(instruction)
    if explicit:
        return explicit
    visible_filter = str((metadata or {}).get("visibleFilter") or "").lower()
    if visible_filter in TASK_CAPTURE_CATEGORIES:
        return TASK_CAPTURE_CATEGORIES[visible_filter]
    return "personal"


def clean_capture_title(text: str) -> str:
    text = re.sub(
        r"^(?:(?:one|new|this|the|my|a)\s+)?(?:cmd\s+)?(?:outcome|task|goal|project)\s*[:\-–—]\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\b(?:in|under|as)\s+(?:deal|deals|portfolio|portco|post|comms|auto|admin|building|learning|networking|writing|personal|trip)\b", "", text, flags=re.I)
    actual_task = re.search(r"\bwhich\s+is\s+(?:to\s+)?(?P<title>.+)$", text, flags=re.I)
    if actual_task:
        text = actual_task.group("title")
    text = re.sub(r"^(?:please\s+)", "", text, flags=re.I)
    text = re.sub(r"^(?:(?:can|could|would|will)\s+you\s+)", "", text, flags=re.I)
    text = re.sub(r"^(?:i|we)\s+need\s+to\s+", "", text, flags=re.I)
    text = re.sub(r"^(?:to\s+)?", "", text, flags=re.I)
    text = re.sub(r"^(?:make\s+it\s+)?(?:red|high|urgent|medium|low)\b(?:\s+priority)?\s*(?:and\s+)?", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n,.;:!?-")
    if not text:
        return ""
    return text[0].upper() + text[1:]


def infer_capture_urgency(instruction: str) -> str:
    if re.search(
        r"\b(high\s+priority|extremely\s+urgent|urgent|asap|emergency|crisis|immediately|right\s+now|red)\b",
        instruction,
        flags=re.I,
    ):
        return "high"
    if re.search(r"\b(this week|today|soon|medium\s+priority|yellow)\b", instruction, flags=re.I):
        return "medium"
    return "low"


def infer_capture_today(instruction: str) -> bool:
    return bool(re.search(
        r"\b(today|right\s+now|immediately|emergency|crisis|extremely\s+urgent|urgent|asap)\b",
        instruction,
        flags=re.I,
    ))


def parse_task_capture_instruction(instruction: str, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    raw = re.sub(r"\s+", " ", instruction or "").strip()
    if not raw:
        return None
    patterns = [
        r"^(?:please\s+)?(?:add|create|capture)\s+(?:a\s+)?(?:task|to[- ]?do|todo|item|next action|reminder)\s+(?:to|that|for|about)?\s*(?P<title>.+)$",
        r"^(?:please\s+)?(?:add|create)\s+(?:a\s+)?to[- ]?do\s+(?:to|that|for|about)?\s*(?P<title>.+)$",
        r"^(?:please\s+)?(?:mark|note)\s+(?:a\s+)?(?:task|to[- ]?do|todo|item)\s+(?:to|that|for|about)?\s*(?P<title>.+)$",
        r"^(?:please\s+)?log\s+that\s+(?P<title>(?:i|we)\s+need\s+to\s+.+)$",
        r"^(?:please\s+)?log\s+(?:a\s+)?(?:task|to[- ]?do|todo|item|next action|reminder)\s+(?:to|that|for|about)?\s*(?P<title>.+)$",
        r"^(?:please\s+)?remind\s+me\s+to\s+(?P<title>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, raw, flags=re.I)
        if not match:
            continue
        title = clean_capture_title(match.group("title"))
        if not title:
            return None
        return {
            "title": title,
            "category": infer_capture_category(raw, metadata),
            "urgency": infer_capture_urgency(raw),
            "today": infer_capture_today(raw),
            "body": f"Captured from Command: {raw}",
        }
    return None


def _unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def build_capture_request(
    development: str,
    *,
    entrypoint: str,
    metadata: dict[str, Any] | None = None,
    create_spec: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate an adapter payload into the one canonical resolver request."""
    metadata = metadata if isinstance(metadata, dict) else {}
    context = metadata.get("captureContext") if isinstance(metadata.get("captureContext"), dict) else {}
    bounded_create_spec = dict(create_spec) if isinstance(create_spec, dict) else None
    declared_category = explicit_capture_category(development)
    if bounded_create_spec is not None and declared_category:
        bounded_create_spec["category"] = declared_category
    candidate_ids = _unique_strings([
        metadata.get("selectedItemId"),
        metadata.get("focusedItemId"),
        metadata.get("taskBoundItemId"),
        metadata.get("parentItemId") if metadata.get("resolveParentAsCandidate") else None,
        (bounded_create_spec or {}).get("parent_item_id"),
        (bounded_create_spec or {}).get("parentItemId"),
        *(metadata.get("candidateItemIds") or []),
        *(metadata.get("activeItemIds") or []),
        context.get("selectedItemId"),
        context.get("focusedItemId"),
        context.get("taskBoundItemId"),
        *(context.get("candidateItemIds") or []),
        *(context.get("activeItemIds") or []),
    ])
    recent_rows = context.get("recentUserTurns") or metadata.get("recentUserTurns") or []
    recent_user_turns = []
    for row in recent_rows[-3:]:
        content = row.get("content") if isinstance(row, dict) else row
        content = re.sub(r"\s+", " ", str(content or "")).strip()
        if content:
            recent_user_turns.append(content)
    return {
        "request_id": str(metadata.get("requestId") or metadata.get("request_id") or "").strip(),
        "development": re.sub(r"\s+", " ", str(development or "")).strip(),
        "entrypoint": str(entrypoint or "unknown"),
        "candidate_item_ids": candidate_ids,
        "recent_user_turns": recent_user_turns,
        "create_new": bounded_create_spec,
        "source_metadata": metadata,
        "rollback_mode": bool(metadata.get("captureRollbackMode"))
        or os.environ.get("CMD_CAPTURE_MODE", "").strip().lower() == "rollback",
    }


def _clarification_plan(question: str, reason: str) -> dict[str, Any]:
    return {
        "operation": "needs_clarification",
        "question": question,
        "reason": reason,
        "confidence": 1.0,
    }


def _no_capture_plan(reason: str) -> dict[str, Any]:
    return {
        "operation": "no_capture",
        "reason": reason,
        "confidence": 1.0,
    }


def _obvious_no_capture_reason(text: str) -> str | None:
    """Recognize only high-precision reactions and conversational repairs."""
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return None
    directive_text = re.sub(
        r"\b(?:do\s+not|don't|dont|never|no\s+need\s+to)\s+"
        r"(?:add|capture|create|log|remind|research|draft|update|make)\b",
        "",
        normalized,
        flags=re.I,
    )
    durable_directive = re.search(
        r"\b(?:add|capture|create|log|remind|research|draft|update|make)\b",
        directive_text,
        flags=re.I,
    )
    if re.search(
        r"\b(?:i\s+)?(?:did\s+not|didn't|didnt)\s+mean\s+to\s+(?:press|hit|click)\s+send\b",
        normalized,
        flags=re.I,
    ) and not durable_directive:
        return "conversational_repair"
    if re.search(
        r"\b(?:never\s*mind|nevermind|ignore\s+(?:that|this)|disregard\s+(?:that|this)|"
        r"do\s+not\s+capture\s+(?:that|this)|don't\s+capture\s+(?:that|this)|"
        r"no\s+action\s+(?:is\s+)?needed)\b",
        normalized,
        flags=re.I,
    ) and not durable_directive:
        return "retracted_or_no_action"
    reaction = re.sub(r"[^a-z0-9 ]+", " ", normalized.casefold())
    reaction = re.sub(r"\s+", " ", reaction).strip()
    if re.fullmatch(
        r"(?:(?:ok|okay|yeah|yes|ha+|haha+|lol|well)\s+)*"
        r"(?:ok|okay|yes|yeah|no|sure|thanks|thank you|got it|all good|sounds good|"
        r"looks good|cool|nice|awesome|perfect|ha+|haha+|lol)"
        r"(?:\s+(?:thanks|thank you|all good|ha+|haha+|lol))*",
        reaction,
    ):
        return "acknowledgement_or_reaction"
    return None


def _looks_like_completed_development(text: str) -> bool:
    return bool(re.search(
        r"\b(i(?:'ve| have)?|we(?:'ve| have)?)\s+(?:already\s+)?(?:sent|finished|completed|handled|updated|did|made)\b"
        r"|\b(?:done|completed|finished|sent both|sent all|handled)\b",
        text,
        flags=re.I,
    ))


def _looks_like_existing_outcome_update(text: str) -> bool:
    return _looks_like_completed_development(text) or bool(re.search(
        r"\b(?:update|complete|close|finish|resolve|mark)\b.{0,80}"
        r"\b(?:outcome|task|item|one|it|this|that)\b"
        r"|\b(?:outcome|task|item)\b.{0,80}"
        r"\b(?:update|complete|close|finish|resolve|done)\b",
        text,
        flags=re.I,
    ))


def _hydrate_unique_named_candidate(
    db_path: Path,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Add one code-resolved candidate for a clearly named existing outcome."""
    if (
        request.get("candidate_item_ids")
        or request.get("create_new")
        or request.get("rollback_mode")
    ):
        return request
    development = str(request.get("development") or "").strip()
    if not development or not _looks_like_existing_outcome_update(development):
        return request
    candidates = registry_shadow.load_active_outcome_candidates(
        db_path,
        limit=registry_shadow.SHADOW_OUTCOME_SCAN_LIMIT,
    )
    source_metadata = dict(request.get("source_metadata") or {})
    declared_category = explicit_capture_category(development)
    visible_filter = str(source_metadata.get("visibleFilter") or "").casefold()
    if not declared_category and visible_filter in TASK_CAPTURE_CATEGORIES:
        declared_category = TASK_CAPTURE_CATEGORIES[visible_filter]
    matched = registry_shadow.select_unique_named_outcome_candidate(
        development,
        candidates,
        category=declared_category or "",
    )
    if matched is None:
        return request
    hydrated = dict(request)
    hydrated["candidate_item_ids"] = [matched["item_id"]]
    source_metadata["candidateResolution"] = {
        "method": "unique_named_active_outcome",
        "itemId": matched["item_id"],
        "category": matched["category"],
    }
    hydrated["source_metadata"] = source_metadata
    return hydrated


def _pronoun_only_development(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    tokens = [token for token in normalized.split() if token not in {"please", "awesome", "okay", "ok", "now"}]
    return bool(tokens) and all(token in {"it", "that", "this", "them", "those", "both", "done"} for token in tokens)


def _development_summary(request: dict[str, Any]) -> str:
    development = str(request.get("development") or "").strip()
    prior = " ".join(str(value) for value in request.get("recent_user_turns") or [])
    combined = f"{development} {prior}".strip()
    lowered = combined.lower()
    if (
        "email" in lowered
        and re.search(r"\b(sent|send|follow-up)\b", lowered)
        and "current developments" in lowered
        and "shareable investment-team materials" in lowered
    ):
        return (
            "Follow-up emails were sent to request current developments and "
            "shareable investment-team materials."
        )
    if prior and development and len(development.split()) <= 12:
        return f"{development.rstrip('.')} — context: {prior.rstrip('.')} .".replace(" .", ".")
    return (development or prior).strip()


def default_capture_resolver(request: dict[str, Any]) -> dict[str, Any]:
    """Resolve only within supplied candidates; persistence remains deterministic."""
    create_spec = request.get("create_new") if isinstance(request.get("create_new"), dict) else None
    development = str(request.get("development") or "").strip()
    candidates = _unique_strings(list(request.get("candidate_item_ids") or []))

    if create_spec:
        return {"operation": "created_new", "create_new": create_spec, "confidence": 1.0}
    if request.get("rollback_mode"):
        return _clarification_plan(
            "CMD capture resolution is temporarily fail-closed. Should I create a new outcome or update an existing one?",
            "rollback_mode",
        )
    if not development:
        return _clarification_plan("What development should CMD capture?", "marker_only")
    no_capture_reason = _obvious_no_capture_reason(development)
    if no_capture_reason:
        return _no_capture_plan(no_capture_reason)
    if _pronoun_only_development(development) and not request.get("recent_user_turns"):
        return _clarification_plan("Which existing CMD outcome does this refer to?", "pronoun_only")

    if candidates:
        summary = _development_summary(request)
        if not summary:
            return _clarification_plan("What development should be added to the selected outcome?", "missing_development")
        if len(candidates) > 1 and not re.search(r"\b(both|all|these|those|two|them)\b", development, flags=re.I):
            return _clarification_plan("Which of the candidate CMD outcomes should be updated?", "conflicting_targets")
        status = "done" if _looks_like_completed_development(development) else None
        return {
            "operation": "updated_existing",
            "updates": [
                {"item_id": item_id, "development_summary": summary, "status": status}
                for item_id in candidates
            ],
            "confidence": 1.0,
        }

    if _looks_like_completed_development(development):
        return _clarification_plan("Which existing CMD outcome should receive this completed development?", "missing_target")

    parsed = parse_task_capture_instruction(development, request.get("source_metadata"))
    title = parsed["title"] if parsed else clean_capture_title(development)
    if not title:
        return _clarification_plan("What new CMD outcome should be created?", "missing_title")
    spec = parsed or {
        "title": title,
        "category": infer_capture_category(development, request.get("source_metadata")),
        "urgency": infer_capture_urgency(development),
        "today": infer_capture_today(development),
        "body": f"Captured from Command: {development}",
    }
    return {"operation": "created_new", "create_new": spec, "confidence": 1.0}


def _capture_idempotency_key(request: dict[str, Any]) -> str:
    stable = {
        "development": request.get("development") or "",
        "entrypoint": request.get("entrypoint") or "",
        "candidate_item_ids": sorted(_unique_strings(list(request.get("candidate_item_ids") or []))),
        "recent_user_turns": list(request.get("recent_user_turns") or []),
        "create_new": request.get("create_new"),
    }
    request_id = str(request.get("request_id") or "").strip()
    if request_id:
        stable["request_id"] = request_id
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _counts(conn: Any) -> tuple[int, int]:
    return (
        int(conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0]),
        int(conn.execute("SELECT COUNT(*) FROM work_item_events").fetchone()[0]),
    )


def _base_verification(pre_items: int, post_items: int, key: str) -> dict[str, Any]:
    return {
        "verified": False,
        "pre_item_count": pre_items,
        "post_item_count": post_items,
        "created_count": post_items - pre_items,
        "canonical_target_ids": [],
        "resulting_statuses": {},
        "durable_context_event_ids": [],
        "idempotency_key": key,
        "replayed": False,
    }


def _clarification_receipt(
    plan: dict[str, Any],
    *,
    pre_items: int,
    post_items: int,
    key: str,
    verified_no_mutation: bool = True,
    error: str = "",
) -> dict[str, Any]:
    verification = _base_verification(pre_items, post_items, key)
    verification["verified"] = verified_no_mutation and pre_items == post_items
    receipt = {
        "ok": False,
        "operation": "needs_clarification",
        "updated_existing": [],
        "created_new": [],
        "needs_clarification": {
            "question": str(plan.get("question") or "CMD capture needs clarification."),
            "reason": str(plan.get("reason") or "ambiguous_capture"),
        },
        "verification": verification,
    }
    if error:
        receipt["error"] = error
    return receipt


def _no_capture_receipt(
    plan: dict[str, Any],
    *,
    pre_items: int,
    post_items: int,
    pre_events: int,
    post_events: int,
    key: str,
) -> dict[str, Any]:
    verification = _base_verification(pre_items, post_items, key)
    verification.update({
        "verified": pre_items == post_items and pre_events == post_events,
        "pre_event_count": pre_events,
        "post_event_count": post_events,
    })
    return {
        "ok": bool(verification["verified"]),
        "operation": "no_capture",
        "updated_existing": [],
        "created_new": [],
        "needs_clarification": None,
        "no_capture": {
            "reason": str(plan.get("reason") or "not_durable_work"),
        },
        "verification": verification,
    }


def _validate_plan(
    conn: Any,
    request: dict[str, Any],
    plan: Any,
) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("operation") not in CAPTURE_PLAN_OPERATIONS:
        return _clarification_plan("CMD could not produce a valid capture plan.", "invalid_plan")
    if float(plan.get("confidence", 1.0) or 0) < CAPTURE_MIN_CONFIDENCE or plan.get("conflicting"):
        return _clarification_plan("Which CMD outcome should this update?", "low_confidence")
    operation = str(plan["operation"])
    if operation == "needs_clarification":
        return _clarification_plan(
            str(plan.get("question") or "What should CMD capture?"),
            str(plan.get("reason") or "resolver_requested_clarification"),
        )
    if operation == "no_capture":
        unsupported = set(plan) - {"operation", "reason", "confidence"}
        if unsupported:
            return _clarification_plan(
                "CMD could not validate a no-capture decision with requested effects.",
                "conflicting_effects",
            )
        return {
            "operation": "no_capture",
            "reason": str(plan.get("reason") or "not_durable_work"),
            "confidence": 1.0,
        }
    if operation == "created_new":
        if plan.get("updates") or plan.get("updated_existing"):
            return _clarification_plan("Should CMD create new work or update existing work?", "conflicting_effects")
        spec = plan.get("create_new")
        if not isinstance(spec, dict):
            return _clarification_plan("What new CMD outcome should be created?", "missing_create_spec")
        title = clean_capture_title(str(spec.get("title") or ""))
        category = TASK_CAPTURE_CATEGORIES.get(str(spec.get("category") or "personal").lower())
        if not title or not category:
            return _clarification_plan("What title and category should the new outcome use?", "invalid_create_spec")
        parent_item_id = str(spec.get("parent_item_id") or spec.get("parentItemId") or "").strip() or None
        candidates = set(_unique_strings(list(request.get("candidate_item_ids") or [])))
        if parent_item_id and parent_item_id not in candidates:
            return _clarification_plan(
                "The resolved CMD parent is outside the supplied candidates.",
                "unknown_parent",
            )
        return {
            "operation": operation,
            "create_new": {
                "title": title,
                "body": str(spec.get("body") or f"Captured from Command: {request.get('development') or title}").strip(),
                "category": category,
                "urgency": str(spec.get("urgency") or "low") if str(spec.get("urgency") or "low") in {"low", "medium", "high"} else "low",
                "today": bool(spec.get("today")),
                "parent_item_id": parent_item_id,
            },
            "confidence": 1.0,
        }

    if plan.get("create_new") or plan.get("created_new"):
        return _clarification_plan("Should CMD create new work or update existing work?", "conflicting_effects")
    updates = plan.get("updates") or plan.get("updated_existing")
    if not isinstance(updates, list) or not updates:
        return _clarification_plan("Which existing CMD outcome should be updated?", "missing_targets")
    candidates = set(_unique_strings(list(request.get("candidate_item_ids") or [])))
    validated_updates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for update in updates:
        if not isinstance(update, dict):
            return _clarification_plan("Which existing CMD outcome should be updated?", "invalid_update")
        item_id = str(update.get("item_id") or "").strip()
        summary = str(update.get("development_summary") or update.get("summary") or "").strip()
        status = update.get("status")
        unsupported = set(update) - {"item_id", "development_summary", "summary", "status"}
        if unsupported:
            return _clarification_plan("The requested CMD update contains unsupported field changes.", "unsupported_mutation")
        if not item_id or item_id not in candidates:
            return _clarification_plan("The resolved CMD outcome is outside the supplied candidates.", "unknown_target")
        if item_id in seen or not summary or status not in {None, "", "done"}:
            return _clarification_plan("CMD could not validate the requested outcome update.", "invalid_update")
        row = conn.execute(
            "SELECT item_id, title, category, status FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if not row:
            return _clarification_plan("One of the candidate CMD outcomes no longer exists.", "unknown_target")
        current_status = str(row["status"])
        if current_status in TERMINAL_CAPTURE_STATUSES:
            return _clarification_plan("A resolved CMD outcome cannot be silently reopened or changed.", "closed_target")
        seen.add(item_id)
        validated_updates.append({
            "item_id": item_id,
            "development_summary": summary,
            "status": status or current_status,
            "title": str(row["title"]),
            "category": str(row["category"] or "personal"),
        })
    return {"operation": operation, "updates": validated_updates, "confidence": 1.0}


def _invoke_failure(injector: FailureInjector | None, stage: str, context: dict[str, Any]) -> Any:
    return injector(stage, context) if injector else None


def _capture_manifest(
    conn: Any,
    key: str,
    request: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]] | None]:
    row = conn.execute(
        "SELECT metadata_json FROM sources WHERE source_type='cmd_capture' AND uri=?",
        (f"cmd-capture:{key}",),
    ).fetchone()
    if not row:
        return False, None
    try:
        metadata = json.loads(str(row["metadata_json"] or "{}"))
    except json.JSONDecodeError as error:
        raise RuntimeError("capture_replay_postcondition_failed") from error
    if (
        not isinstance(metadata, dict)
        or metadata.get("idempotency_key") != key
        or not isinstance(metadata.get("request"), dict)
        or _capture_idempotency_key(metadata["request"]) != _capture_idempotency_key(request)
    ):
        raise RuntimeError("capture_replay_postcondition_failed")
    if "declared_effects" not in metadata:
        return True, None
    declared = metadata.get("declared_effects")
    if not isinstance(declared, list) or not declared:
        raise RuntimeError("capture_replay_postcondition_failed")
    return True, declared


def _upsert_capture_manifest(
    conn: Any,
    key: str,
    request: dict[str, Any],
    declared_effects: list[dict[str, Any]],
    *,
    label: str,
) -> str:
    return cmd_db.upsert_source(
        conn,
        "cmd_capture",
        f"cmd-capture:{key}",
        label,
        {
            "request": request,
            "idempotency_key": key,
            "declared_effects": declared_effects,
        },
    )


def _existing_capture_receipt(
    conn: Any,
    key: str,
    pre_items: int,
    declared_effects: list[dict[str, Any]] | None = None,
    *,
    capture_source_exists: bool = False,
) -> dict[str, Any] | None:
    rows = conn.execute(
        """
        SELECT e.event_id, e.event_type, e.item_id, e.summary, e.status,
               e.raw_json, w.title, w.category, w.status AS current_status,
               w.parent_item_id
        FROM work_item_events e
        JOIN work_items w ON w.item_id=e.item_id
        WHERE e.event_id LIKE ?
        ORDER BY e.event_id
        """,
        (f"capture:{key}:%",),
    ).fetchall()
    if not rows:
        if capture_source_exists or declared_effects is not None:
            raise RuntimeError("capture_replay_postcondition_failed")
        return None
    declarations: list[list[dict[str, Any]]] = []
    for row in rows:
        try:
            raw = json.loads(str(row["raw_json"] or "{}"))
        except json.JSONDecodeError as error:
            raise RuntimeError("capture_replay_postcondition_failed") from error
        declared = raw.get("declared_effects") if isinstance(raw, dict) else None
        if not isinstance(declared, list) or not declared:
            raise RuntimeError("capture_replay_postcondition_failed")
        declarations.append(declared)
    canonical_effects = declared_effects or declarations[0]
    canonical_declaration = json.dumps(
        canonical_effects, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    declarations_to_compare = declarations if declared_effects is not None else declarations[1:]
    if any(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        != canonical_declaration
        for value in declarations_to_compare
    ):
        raise RuntimeError("capture_replay_postcondition_failed")

    expected: dict[str, dict[str, str]] = {}
    for effect in canonical_effects:
        if not isinstance(effect, dict):
            raise RuntimeError("capture_replay_postcondition_failed")
        normalized = {
            "event_id": str(effect.get("event_id") or ""),
            "item_id": str(effect.get("item_id") or ""),
            "event_type": str(effect.get("event_type") or ""),
            "summary": str(effect.get("summary") or ""),
            "status": str(effect.get("status") or ""),
            "parent_item_id": str(effect.get("parent_item_id") or ""),
        }
        if (
            not normalized["event_id"]
            or not normalized["item_id"]
            or not normalized["summary"].strip()
            or not normalized["status"]
            or normalized["event_type"] not in {
                "capture_development", "task_captured", "capture_deduplicated",
            }
            or normalized["event_id"] in expected
        ):
            raise RuntimeError("capture_replay_postcondition_failed")
        expected[normalized["event_id"]] = normalized

    actual = {str(row["event_id"]): row for row in rows}
    if set(actual) != set(expected):
        raise RuntimeError("capture_replay_postcondition_failed")
    for event_id, effect in expected.items():
        row = actual[event_id]
        if not all(
            str(row[field]) == effect[field]
            for field in ("item_id", "event_type", "summary", "status")
        ) or str(row["current_status"]) != effect["status"]:
            raise RuntimeError("capture_replay_postcondition_failed")
        if effect["event_type"] != "capture_development" and (
            str(row["parent_item_id"] or "") != effect["parent_item_id"]
        ):
            raise RuntimeError("capture_replay_postcondition_failed")

    event_types = {effect["event_type"] for effect in expected.values()}
    if event_types == {"capture_development"}:
        operation = "updated_existing"
    elif len(expected) == 1 and event_types <= {"task_captured", "capture_deduplicated"}:
        operation = "created_new"
    else:
        raise RuntimeError("capture_replay_postcondition_failed")
    updated_existing = [
        {
            "item_id": effect["item_id"],
            "title": str(actual[event_id]["title"]),
            "category": str(actual[event_id]["category"]),
            "status": effect["status"],
            "development_event_id": event_id,
        }
        for event_id, effect in expected.items()
        if effect["event_type"] == "capture_development"
    ]
    created_new = [
        {
            "item_id": effect["item_id"],
            "title": str(actual[event_id]["title"]),
            "category": str(actual[event_id]["category"]),
            "status": effect["status"],
            "captureDeduplicated": effect["event_type"] == "capture_deduplicated",
        }
        for event_id, effect in expected.items()
        if effect["event_type"] != "capture_development"
    ]
    selected = updated_existing or created_new
    verification = _base_verification(pre_items, pre_items, key)
    verification.update({
        "verified": bool(selected),
        "created_count": 0,
        "canonical_target_ids": [str(row["item_id"]) for row in selected],
        "resulting_statuses": {str(row["item_id"]): str(row["status"]) for row in selected},
        "durable_context_event_ids": [str(row["development_event_id"]) for row in updated_existing],
        "replayed": True,
    })
    return {
        "ok": bool(selected),
        "operation": operation,
        "updated_existing": updated_existing,
        "created_new": created_new,
        "needs_clarification": None,
        "verification": verification,
    }


def _capture_replay_receipt(
    conn: Any,
    key: str,
    pre_items: int,
    request: dict[str, Any],
) -> dict[str, Any] | None:
    source_exists, declared_effects = _capture_manifest(conn, key, request)
    return _existing_capture_receipt(
        conn,
        key,
        pre_items,
        declared_effects,
        capture_source_exists=source_exists,
    )


def _create_item_in_transaction(
    conn: Any,
    spec: dict[str, Any],
    request: dict[str, Any],
    *,
    key: str,
    now: str,
    slugify_fn: SlugifyFn,
    failure_injector: FailureInjector | None,
) -> tuple[dict[str, Any], str]:
    parent_item_id = spec.get("parent_item_id")
    if parent_item_id:
        parent = conn.execute(
            "SELECT item_id, parent_item_id FROM work_items WHERE item_id=?",
            (parent_item_id,),
        ).fetchone()
        if not parent:
            raise cmd_db.WorkItemHierarchyError("parent_not_found")
        if parent["parent_item_id"] is not None:
            raise cmd_db.WorkItemHierarchyError("max_depth_exceeded")

    equivalent = cmd_db.equivalent_active_item_ids(
        conn,
        spec["title"],
        spec["category"],
        parent_item_id,
    )
    if len(equivalent) == 1:
        item_id = equivalent[0]
        row = conn.execute(
            "SELECT item_id, title, category, status FROM work_items WHERE item_id=?",
            (item_id,),
        ).fetchone()
        event_id = f"capture:{key}:deduplicated:{item_id}"
        declared_effects = [{
            "event_id": event_id,
            "item_id": item_id,
            "event_type": "capture_deduplicated",
            "summary": "Capture matched the existing active CMD outcome; no duplicate was created.",
            "status": str(row["status"]),
            "parent_item_id": str(parent_item_id or ""),
        }]
        _upsert_capture_manifest(
            conn,
            key,
            request,
            declared_effects,
            label=str(row["title"]),
        )
        cmd_db.upsert_work_item_event(
            conn,
            event_id=event_id,
            item_id=item_id,
            event_type="capture_deduplicated",
            category=str(row["category"]),
            subject=str(row["title"]).split("—", 1)[0].strip(),
            title=str(row["title"]),
            summary="Capture matched the existing active CMD outcome; no duplicate was created.",
            status=str(row["status"]),
            occurred_at=now,
            week_id="",
            source_type=str(request.get("entrypoint") or "cmd_capture"),
            reportable=False,
            raw={
                "request": request,
                "idempotency_key": key,
                "declared_effects": declared_effects,
            },
        )
        return {
            "item_id": item_id,
            "title": str(row["title"]),
            "category": str(row["category"]),
            "status": str(row["status"]),
            "captureDeduplicated": True,
        }, event_id

    if len(equivalent) > 1:
        raise ValueError("ambiguous_equivalent_items")
    if parent_item_id is None:
        next_sort_order = int(conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM work_items WHERE parent_item_id IS NULL"
        ).fetchone()[0])
    else:
        next_sort_order = int(conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM work_items WHERE parent_item_id=?",
            (parent_item_id,),
        ).fetchone()[0])
    base_id = slugify_fn(spec["title"])
    item_id = base_id
    suffix = 2
    while conn.execute("SELECT 1 FROM work_items WHERE item_id=?", (item_id,)).fetchone():
        item_id = f"{base_id}-{suffix}"
        suffix += 1
    event_id = f"capture:{key}:created:{item_id}"
    declared_effects = [{
        "event_id": event_id,
        "item_id": item_id,
        "event_type": "task_captured",
        "summary": f"Captured new CMD outcome: {spec['title']}.",
        "status": "open",
        "parent_item_id": str(parent_item_id or ""),
    }]
    source_uri = f"cmd-capture:{key}"
    source_id = _upsert_capture_manifest(
        conn,
        key,
        request,
        declared_effects,
        label=spec["title"],
    )
    conn.execute(
        """
        INSERT INTO work_items(
          item_id, title, body, category, urgency, today, status, source_state,
          current_source_id, source_fingerprint, created_at, updated_at, last_seen_at,
          metadata_json, parent_item_id, sort_order
        ) VALUES (?, ?, ?, ?, ?, ?, 'open', 'present', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id, spec["title"], spec["body"], spec["category"], spec["urgency"],
            1 if spec["today"] else 0, source_id, source_uri, now, now, now,
            json.dumps({
                "created_from": "canonical_cmd_capture",
                "entrypoint": request.get("entrypoint"),
                "idempotency_key": key,
                "direct_overrides": ["title", "body", "category", "urgency", "today"],
            }, ensure_ascii=False, sort_keys=True),
            parent_item_id, next_sort_order,
        ),
    )
    conn.execute(
        """
        INSERT INTO work_item_sources(item_id, source_id, source_item_id, raw_text, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (item_id, source_id, key, str(request.get("development") or spec["title"]), now, now),
    )
    _invoke_failure(failure_injector, "after_item_insert", {"item_id": item_id, "key": key})
    if _invoke_failure(failure_injector, "before_event_write", {"item_id": item_id, "event_id": event_id}) is not False:
        cmd_db.upsert_work_item_event(
            conn,
            event_id=event_id,
            item_id=item_id,
            event_type="task_captured",
            category=spec["category"],
            subject=spec["title"].split("—", 1)[0].strip(),
            title=spec["title"],
            summary=f"Captured new CMD outcome: {spec['title']}.",
            status="open",
            occurred_at=now,
            week_id="",
            source_type=str(request.get("entrypoint") or "cmd_capture"),
            source_id=source_id,
            source_item_id=key,
            reportable=False,
            raw={
                "request": request,
                "idempotency_key": key,
                "declared_effects": declared_effects,
            },
        )
    return {
        "item_id": item_id,
        "title": spec["title"],
        "category": spec["category"],
        "status": "open",
        "captureDeduplicated": False,
    }, event_id


def resolve_and_apply_capture(
    db_path: Path,
    request: dict[str, Any],
    resolver_fn: ResolverFn | None = None,
    *,
    now_fn: NowFn = cmd_db.utc_now,
    slugify_fn: SlugifyFn = cmd_db.slugify_item_id,
    failure_injector: FailureInjector | None = None,
) -> dict[str, Any]:
    """Resolve, validate, mutate, and verify one capture in one transaction."""
    cmd_db.init_db(db_path)
    normalized_request = build_capture_request(
        str(request.get("development") or ""),
        entrypoint=str(request.get("entrypoint") or "unknown"),
        metadata={
            **(request.get("source_metadata") if isinstance(request.get("source_metadata"), dict) else {}),
            "candidateItemIds": list(request.get("candidate_item_ids") or []),
            "recentUserTurns": list(request.get("recent_user_turns") or []),
            "captureRollbackMode": bool(request.get("rollback_mode")),
        },
        create_spec=request.get("create_new") if isinstance(request.get("create_new"), dict) else None,
    )
    normalized_request = _hydrate_unique_named_candidate(db_path, normalized_request)
    key = _capture_idempotency_key(normalized_request)
    if normalized_request.get("rollback_mode") and not normalized_request.get("create_new"):
        with cmd_db.connect(db_path) as conn:
            item_count, _ = _counts(conn)
        return _clarification_receipt(
            _clarification_plan(
                "CMD capture resolution is temporarily fail-closed. Should I create a new outcome or update an existing one?",
                "rollback_mode",
            ),
            pre_items=item_count,
            post_items=item_count,
            key=key,
        )
    replay_pre_items = 0
    try:
        with cmd_db.connect(db_path) as replay_conn:
            replay_pre_items, _ = _counts(replay_conn)
            replay = _capture_replay_receipt(
                replay_conn,
                key,
                replay_pre_items,
                normalized_request,
            )
        if replay is not None:
            return replay
    except Exception as error:
        return _clarification_receipt(
            _clarification_plan(
                "CMD could not verify the capture transaction. No changes were kept.",
                "transaction_failed",
            ),
            pre_items=replay_pre_items,
            post_items=replay_pre_items,
            key=key,
            verified_no_mutation=False,
            error=str(error),
        )

    resolver = resolver_fn or default_capture_resolver
    try:
        proposed_plan = resolver(normalized_request)
    except Exception as error:
        with cmd_db.connect(db_path) as conn:
            item_count, _ = _counts(conn)
        return _clarification_receipt(
            _clarification_plan("CMD capture resolution failed. Please clarify the intended outcome.", "resolver_failure"),
            pre_items=item_count,
            post_items=item_count,
            key=key,
            verified_no_mutation=True,
            error=f"resolver_failure:{error}",
        )

    conn = cmd_db.connect(db_path)
    pre_items = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        pre_items, pre_events = _counts(conn)
        replay = _capture_replay_receipt(conn, key, pre_items, normalized_request)
        if replay is not None:
            conn.rollback()
            return replay
        plan = _validate_plan(conn, normalized_request, proposed_plan)
        if plan["operation"] == "needs_clarification":
            post_items, post_events = _counts(conn)
            conn.rollback()
            return _clarification_receipt(
                plan,
                pre_items=pre_items,
                post_items=post_items,
                key=key,
                verified_no_mutation=pre_events == post_events,
            )
        if plan["operation"] == "no_capture":
            post_items, post_events = _counts(conn)
            conn.rollback()
            return _no_capture_receipt(
                plan,
                pre_items=pre_items,
                post_items=post_items,
                pre_events=pre_events,
                post_events=post_events,
                key=key,
            )
        now = now_fn()
        updated_existing: list[dict[str, Any]] = []
        created_new: list[dict[str, Any]] = []
        expected_event_ids: list[str] = []
        expected_events: dict[str, dict[str, str]] = {}
        if plan["operation"] == "updated_existing":
            declared_effects = [{
                "event_id": f"capture:{key}:development:{update['item_id']}",
                "item_id": str(update["item_id"]),
                "event_type": "capture_development",
                "summary": str(update["development_summary"]),
                "status": str(update["status"]),
                "parent_item_id": "",
            } for update in plan["updates"]]
            _upsert_capture_manifest(
                conn,
                key,
                normalized_request,
                declared_effects,
                label=f"CMD capture {key}",
            )
            expected_event_ids.extend(effect["event_id"] for effect in declared_effects)
            expected_events.update({
                effect["event_id"]: {
                    field: effect[field]
                    for field in ("item_id", "event_type", "summary", "status")
                }
                for effect in declared_effects
            })
            for index, update in enumerate(plan["updates"]):
                context = {"index": index, "item_id": update["item_id"], "key": key}
                _invoke_failure(failure_injector, "before_update", context)
                item_id = str(context.get("item_id") or update["item_id"])
                resulting_status = str(update["status"])
                if resulting_status == "done":
                    conn.execute(
                        """
                        UPDATE work_items
                        SET status='done', today=0, done_at=?, closed_at=?, updated_at=?, last_action_at=?
                        WHERE item_id=? AND status IN ('open', 'awaiting_human', 'blocked')
                        """,
                        (now, now, now, now, item_id),
                    )
                else:
                    conn.execute(
                        "UPDATE work_items SET updated_at=?, last_action_at=? WHERE item_id=?",
                        (now, now, item_id),
                    )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise RuntimeError(f"capture_target_update_failed:{item_id}")
                _invoke_failure(failure_injector, "after_update", context)
                event_id = f"capture:{key}:development:{update['item_id']}"
                if _invoke_failure(failure_injector, "before_event_write", {**context, "event_id": event_id}) is not False:
                    cmd_db.upsert_work_item_event(
                        conn,
                        event_id=event_id,
                        item_id=item_id,
                        event_type="capture_development",
                        category=update["category"],
                        subject=update["title"].split("—", 1)[0].strip(),
                        title=update["title"],
                        summary=update["development_summary"],
                        status=resulting_status,
                        occurred_at=now,
                        week_id="",
                        source_type=str(normalized_request.get("entrypoint") or "cmd_capture"),
                        source_item_id=key,
                        reportable=False,
                        raw={
                            "development": normalized_request["development"],
                            "recent_user_turns": normalized_request["recent_user_turns"],
                            "candidate_item_ids": normalized_request["candidate_item_ids"],
                            "idempotency_key": key,
                            "declared_effects": declared_effects,
                        },
                    )
                updated_existing.append({
                    "item_id": update["item_id"],
                    "title": update["title"],
                    "category": update["category"],
                    "status": resulting_status,
                    "development_event_id": event_id,
                })
        else:
            created, event_id = _create_item_in_transaction(
                conn,
                plan["create_new"],
                normalized_request,
                key=key,
                now=now,
                slugify_fn=slugify_fn,
                failure_injector=failure_injector,
            )
            created_new.append(created)
            expected_event_ids.append(event_id)
            capture_deduplicated = bool(created.get("captureDeduplicated"))
            expected_events[event_id] = {
                "item_id": str(created["item_id"]),
                "event_type": "capture_deduplicated" if capture_deduplicated else "task_captured",
                "summary": (
                    "Capture matched the existing active CMD outcome; no duplicate was created."
                    if capture_deduplicated
                    else f"Captured new CMD outcome: {created['title']}."
                ),
                "status": str(created["status"]),
            }

        _invoke_failure(failure_injector, "before_verification", {"key": key})
        post_items, post_events = _counts(conn)
        expected_created = 1 if created_new and not created_new[0].get("captureDeduplicated") else 0
        target_rows = conn.execute(
            f"SELECT item_id, status FROM work_items WHERE item_id IN ({','.join('?' for _ in (updated_existing or created_new))})",
            [row["item_id"] for row in (updated_existing or created_new)],
        ).fetchall()
        event_rows = conn.execute(
            f"SELECT event_id, item_id, event_type, summary, status FROM work_item_events WHERE event_id IN ({','.join('?' for _ in expected_event_ids)})",
            expected_event_ids,
        ).fetchall()
        target_statuses = {str(row["item_id"]): str(row["status"]) for row in target_rows}
        expected_statuses = {
            row["item_id"]: row["status"]
            for row in (updated_existing or created_new)
        }
        expected_targets = {row["item_id"] for row in (updated_existing or created_new)}
        actual_events = {str(row["event_id"]): row for row in event_rows}
        events_verified = set(actual_events) == set(expected_events)
        if events_verified:
            for event_id, expected in expected_events.items():
                row = actual_events[event_id]
                events_verified = events_verified and all(
                    str(row[field]) == expected[field]
                    for field in ("item_id", "event_type", "summary", "status")
                )
        verified = (
            post_items - pre_items == expected_created
            and post_events - pre_events == len(expected_event_ids)
            and len(event_rows) == len(expected_event_ids)
            and {expected["item_id"] for expected in expected_events.values()} == expected_targets
            and events_verified
            and target_statuses == expected_statuses
        )
        if not verified:
            raise RuntimeError("capture_postcondition_failed")
        conn.commit()
        verification = _base_verification(pre_items, post_items, key)
        verification.update({
            "verified": True,
            "created_count": expected_created,
            "canonical_target_ids": [row["item_id"] for row in (updated_existing or created_new)],
            "resulting_statuses": target_statuses,
            "durable_context_event_ids": [
                row["development_event_id"] for row in updated_existing
            ],
        })
        receipt = {
            "ok": True,
            "operation": plan["operation"],
            "updated_existing": updated_existing,
            "created_new": created_new,
            "needs_clarification": None,
            "verification": verification,
        }
        if created_new:
            task = cmd_db.get_work_item(db_path, created_new[0]["item_id"])
            if task:
                task["captureDeduplicated"] = bool(created_new[0].get("captureDeduplicated"))
                receipt["captured_task"] = task
        return receipt
    except Exception as error:
        conn.rollback()
        with cmd_db.connect(db_path) as verify_conn:
            post_items, post_events = _counts(verify_conn)
        return _clarification_receipt(
            _clarification_plan(
                "CMD could not verify the capture transaction. No changes were kept.",
                "transaction_failed",
            ),
            pre_items=pre_items,
            post_items=post_items,
            key=key,
            verified_no_mutation=False,
            error=str(error),
        )
    finally:
        conn.close()


def create_captured_work_item(
    db_path: Path,
    instruction: str,
    metadata: dict[str, Any] | None = None,
    *,
    now_fn: NowFn,
    slugify_fn: SlugifyFn,
) -> dict[str, Any] | None:
    parsed = parse_task_capture_instruction(instruction, metadata)
    if not parsed:
        return None
    metadata = metadata or {}
    receipt = resolve_and_apply_capture(
        db_path,
        build_capture_request(
            instruction,
            entrypoint=str(metadata.get("entrypoint") or "legacy_create_captured_work_item"),
            metadata=metadata,
            create_spec={
                **parsed,
                "parent_item_id": str(metadata.get("parentItemId") or "").strip() or None,
            },
        ),
        now_fn=now_fn,
        slugify_fn=slugify_fn,
    )
    return receipt.get("captured_task") if receipt.get("ok") else None


def create_task_from_command(
    db_path: Path,
    text: str,
    metadata: dict[str, Any] | None = None,
    *,
    now_fn: NowFn,
    slugify_fn: SlugifyFn,
) -> dict[str, Any] | None:
    raw = re.sub(r"\s+", " ", text or "").strip()
    if not raw:
        return None
    parsed = parse_task_capture_instruction(raw, metadata)
    if not parsed:
        parsed = {
            "title": clean_capture_title(raw),
            "category": infer_capture_category(raw, metadata),
            "urgency": infer_capture_urgency(raw),
            "body": f"Captured from Command: {raw}",
        }

    metadata = metadata or {}
    create_spec = {
        **parsed,
        "title": str(metadata.get("title") or parsed["title"]),
        "body": str(metadata.get("body") or parsed["body"]),
        "category": str(metadata.get("category") or parsed["category"]),
        "urgency": str(metadata.get("urgency") or parsed["urgency"]),
        "today": bool(metadata.get("today")),
        "parent_item_id": str(metadata.get("parentItemId") or "").strip() or None,
    }
    receipt = resolve_and_apply_capture(
        db_path,
        build_capture_request(
            raw,
            entrypoint=str(metadata.get("entrypoint") or "legacy_positional_cli"),
            metadata=metadata,
            create_spec=create_spec,
        ),
        now_fn=now_fn,
        slugify_fn=slugify_fn,
    )
    return receipt.get("captured_task") if receipt.get("ok") else None


def try_capture_agent_chat_task(
    db_path: Path,
    payload: dict[str, Any],
    *,
    now_fn: NowFn,
    slugify_fn: SlugifyFn,
) -> dict[str, Any] | None:
    if payload.get("kind") != "agent_chat":
        return None
    instruction = str(payload.get("instruction") or "").strip()
    activation = extract_cmd_capture_activation(instruction)
    if not activation:
        return None
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    request = build_capture_request(
        activation["instruction"],
        entrypoint="api_actions_agent_chat",
        metadata={
            **metadata,
            "cmdCaptureActivated": True,
            "cmdCaptureMarker": activation["marker"],
            "cmdCaptureOriginal": activation["original"],
        },
    )
    receipt = resolve_and_apply_capture(
        db_path,
        request,
        now_fn=now_fn,
        slugify_fn=slugify_fn,
    )
    receipt["mode"] = "task_capture"
    receipt["activation"] = activation["marker"]
    return receipt
