"""Observation-only bridge from canonical CMD capture to Resolver Center."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from cmd_runtime.resolution import ResolverRegistry

from . import registry_shadow


def observation_enabled(state_dir: Path) -> bool:
    """Return whether this CMD state directory is collecting resolver evidence."""
    if os.environ.get("CMD_REGISTRY_RESOLVER_SHADOW", "").strip().lower() in {
        "1", "true", "yes",
    }:
        return True
    cohort_path = Path(
        os.environ.get("CMD_REGISTRY_SHADOW_COHORT", "")
        or state_dir / "resolver-shadow-cohort.json"
    ).expanduser()
    if not cohort_path.exists():
        return False
    try:
        cohort = json.loads(cohort_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(cohort, dict) and cohort.get("status") == "collecting"


def observe_capture_resolution(
    db_path: Path,
    request: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, str] | None:
    """Append one bounded capture observation without changing capture success."""
    state_dir = db_path.expanduser().parent
    if not observation_enabled(state_dir):
        return None
    instruction = str(request.get("development") or "").strip()
    if not instruction:
        return None

    verification = (
        receipt.get("verification")
        if isinstance(receipt.get("verification"), Mapping)
        else {}
    )
    idempotency_key = str(verification.get("idempotency_key") or "").strip()
    request_id = str(request.get("request_id") or "").strip()
    action_id = _capture_action_id(request_id, idempotency_key, instruction)
    entrypoint = str(request.get("entrypoint") or "unknown").strip() or "unknown"
    # Browser action intake appends a richer envelope after binding and routing.
    if entrypoint in {"api_actions_agent_chat", "api_actions_semantic_outcome_shaper"}:
        return None
    source_metadata = (
        request.get("source_metadata")
        if isinstance(request.get("source_metadata"), Mapping)
        else {}
    )
    available_inputs = source_metadata.get("availableInputs")
    if not isinstance(available_inputs, list):
        available_inputs = source_metadata.get("available_inputs")
    if not isinstance(available_inputs, list):
        available_inputs = []
    action = {
        "id": action_id,
        "request_id": request_id or action_id,
        "kind": "agent_chat" if _is_conversational_entrypoint(entrypoint) else "codex_action",
        "instruction": instruction,
        "recent_user_turns": list(request.get("recent_user_turns") or [])[-3:],
        "available_inputs": available_inputs,
    }

    operation = str(receipt.get("operation") or "needs_clarification")
    current_route = _capture_route(operation)
    candidates = _capture_candidates(
        db_path,
        action,
        verification.get("canonical_target_ids") or [],
    )
    registry = ResolverRegistry.load_with_local_overlay(state_dir)
    connected, granted = registry_shadow.registry_runtime_scope(
        registry,
        _connector_health(state_dir),
    )
    profile: dict[str, Any] = {}
    identity = os.environ.get("CMD_USER_IDENTITY", "").strip()
    if identity:
        profile["identity"] = identity
    envelope = registry_shadow.build_registry_shadow_request(
        action,
        current_route=current_route,
        candidate_outcomes=candidates,
        connected_tool_ids=connected,
        granted_operations=granted,
        profile=profile,
        enqueued_at=observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    envelope["source_entrypoint"] = entrypoint[:100]
    envelope["production_capture"] = {
        "operation": operation,
        "ok": receipt.get("ok") is True,
        "verified": verification.get("verified") is True,
        "canonical_target_ids": [
            str(value)[:256]
            for value in verification.get("canonical_target_ids") or []
            if str(value).strip()
        ],
    }
    queue_path = Path(
        os.environ.get("CMD_REGISTRY_SHADOW_QUEUE", "")
        or state_dir / "resolver-shadow-queue.jsonl"
    ).expanduser()
    registry_shadow.append_jsonl(queue_path, envelope)
    return {"status": "queued", "shadow_id": str(envelope["shadow_id"])}


def _capture_action_id(request_id: str, idempotency_key: str, instruction: str) -> str:
    if request_id:
        digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]
        return f"act-request-{digest}"
    stable = idempotency_key or hashlib.sha256(instruction.encode("utf-8")).hexdigest()[:24]
    return f"act-capture-{stable[:24]}"


def _is_conversational_entrypoint(entrypoint: str) -> bool:
    normalized = entrypoint.casefold()
    return normalized in {"$cmd", "$cmd-capture", "structured_cli"}


def _capture_route(operation: str) -> dict[str, Any]:
    capability = {
        "created_new": "task.create",
        "updated_existing": "task.update",
    }.get(operation, "none")
    return {
        "intent": "canonical_cmd_capture",
        "capability": capability,
        "execution_mode": "capture_only",
        "risk_level": "low",
        "confidence": 1.0,
        "negated": operation == "no_capture",
        "already_done": False,
        "model": "capture_resolver",
    }


def _capture_candidates(
    db_path: Path,
    action: Mapping[str, Any],
    target_ids: Any,
) -> tuple[dict[str, str], ...]:
    active = registry_shadow.load_active_outcome_candidates(
        db_path,
        limit=registry_shadow.SHADOW_OUTCOME_SCAN_LIMIT,
    )
    selected = list(registry_shadow.select_relevant_outcome_candidates(
        str(action.get("instruction") or ""),
        active,
        limit=registry_shadow.SHADOW_OUTCOME_CANDIDATE_LIMIT,
    ))
    targets = {
        str(value).strip() for value in target_ids
        if str(value).strip()
    } if isinstance(target_ids, (list, tuple, set)) else set()
    if not targets:
        return tuple(selected)
    exact = [row for row in active if row["item_id"] in targets]
    remainder = [row for row in selected if row["item_id"] not in targets]
    return tuple((exact + remainder)[:registry_shadow.SHADOW_OUTCOME_CANDIDATE_LIMIT])


def _connector_health(state_dir: Path) -> dict[str, Any]:
    path = state_dir / "connector-health.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
