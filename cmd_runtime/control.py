"""Engine routing and side-effect-free shadow compilation for CMD action intake."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .capabilities import CapabilityRegistry, PolicyDenial
from .context_resolver import resolve_context
from .jobspec import CapabilityGrant, CompiledJob, compile_job
from .state import JobStateStore


LegacyWriter = Callable[[dict[str, Any]], Any]
ContextCatalogProvider = Callable[[Mapping[str, Any]], Any]
REPLAY_SCHEMA_VERSION = "1"
REPLAY_FALLBACK_CREATED_AT = "1970-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class IntakeResult:
    engine: str
    legacy_written: bool
    job_id: str | None
    status: str
    denials: tuple[Mapping[str, str], ...]


@dataclass(frozen=True)
class ShadowReplayRecord:
    """Pure, JSON-serializable evidence for one v1-to-v2 action replay."""

    schema_version: str
    action_id: str
    action_hash: str
    replay_created_at: str
    legacy: Mapping[str, Any]
    shadow: Mapping[str, Any]
    comparison: Mapping[str, Any]
    effects: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "action_hash": self.action_hash,
            "replay_created_at": self.replay_created_at,
            "legacy": _json_clone(self.legacy),
            "shadow": _json_clone(self.shadow),
            "comparison": _json_clone(self.comparison),
            "effects": _json_clone(self.effects),
        }


CAPABILITY_MAP = {
    "task.create": ("task.create", "commit"),
    "local.write": ("local.write", "commit"),
    "web.read": ("web.search", "read"),
    "source.read": ("local.read", "read"),
    "web.read/source.read": ("web.search", "read"),
    "gmail.draft": ("gmail.draft", "prepare"),
    "gmail.send": ("gmail.send", "commit"),
    "calendar.create": ("calendar.create", "commit"),
    "callmemo.execute": ("callmemo.prepare", "prepare"),
}
EXACT_TARGET_REQUIRED = frozenset({"gmail.send", "calendar.create"})


class CmdControlPlane:
    def __init__(
        self,
        store: JobStateStore | None,
        registry: CapabilityRegistry,
        context_catalog_provider: ContextCatalogProvider | None = None,
    ):
        self.store = store
        self.registry = registry
        self.context_catalog_provider = context_catalog_provider

    def ingest(self, action: Mapping[str, Any], legacy_writer: LegacyWriter) -> IntakeResult:
        if self.store is None:
            raise RuntimeError("live state is required for action intake")
        engine = self.store.get_engine()
        action_value = dict(action)
        legacy_written = False
        if engine in {"v1", "v2_shadow"}:
            legacy_writer(action_value)
            legacy_written = True
        if engine == "v1":
            return IntakeResult(engine, legacy_written, None, "legacy_queued", ())
        compiled, compiler_denials, decision = self._compile_and_authorize(action_value)
        self.store.create_job(compiled, engine=engine)
        denials = list(compiler_denials)
        denials.extend(_policy_denial_dict(item) for item in decision.denials)
        if denials:
            self.store.transition(
                compiled.spec.job_id,
                "blocked",
                "shadow_policy_blocked" if engine == "v2_shadow" else "policy_blocked",
                payload={"denials": denials},
                idempotency_key=f"{compiled.spec.job_id}:policy-blocked",
            )
            return IntakeResult(engine, legacy_written, compiled.spec.job_id, "blocked", tuple(denials))
        self.store.transition(
            compiled.spec.job_id,
            "validated",
            "shadow_validated" if engine == "v2_shadow" else "policy_validated",
            payload={"tools": list(decision.tool_names), "side_effects": False if engine == "v2_shadow" else None},
            idempotency_key=f"{compiled.spec.job_id}:validated",
        )
        if engine == "v2":
            needs_approval = any(grant.approval_required and grant.mode == "commit" for grant in compiled.spec.capability_grants)
            target = "awaiting_approval" if needs_approval else "queued"
            self.store.transition(
                compiled.spec.job_id,
                target,
                "approval_required" if needs_approval else "job_queued",
                payload={},
                idempotency_key=f"{compiled.spec.job_id}:{target}",
            )
        return IntakeResult(engine, legacy_written, compiled.spec.job_id, self.store.get_job(compiled.spec.job_id)["status"], ())

    def replay(self, action: Mapping[str, Any]) -> ShadowReplayRecord:
        """Compile and authorize an action without invoking writers, state, or providers."""
        action_value = dict(action)
        replay_created_at = _replay_created_at(action_value)
        compiled, compiler_denials, decision = self._compile_and_authorize(
            action_value,
            created_at=replay_created_at,
        )
        denials = list(compiler_denials)
        denials.extend(_policy_denial_dict(item) for item in decision.denials)
        shadow_accepts = not denials
        approval_required = any(
            grant.approval_required and grant.mode == "commit"
            for grant in compiled.spec.capability_grants
        )
        shadow_status = "validated" if shadow_accepts else "blocked"
        next_status = (
            "blocked"
            if not shadow_accepts
            else "awaiting_approval" if approval_required else "queued"
        )
        requested = [grant.capability for grant in compiled.spec.capability_grants]
        shadow = {
            "status": shadow_status,
            "would_queue_as": next_status if shadow_accepts else None,
            "job_id": compiled.spec.job_id,
            "spec_hash": compiled.spec.spec_hash,
            "requested_capabilities": requested,
            "authorized_tools": list(decision.tool_names) if shadow_accepts else [],
            "approval_required": approval_required if shadow_accepts else False,
            "denials": denials,
            "spec": compiled.spec.to_dict(),
        }
        legacy = {"status": "legacy_queued", "accepts": True}
        comparison = {
            "legacy_accepts": True,
            "shadow_accepts": shadow_accepts,
            "acceptance_changed": not shadow_accepts,
            "classification": "tightened" if not shadow_accepts else "preserves_acceptance",
        }
        return ShadowReplayRecord(
            schema_version=REPLAY_SCHEMA_VERSION,
            action_id=str(action_value.get("id") or "action-intake"),
            action_hash=_action_hash(action_value),
            replay_created_at=replay_created_at,
            legacy=legacy,
            shadow=shadow,
            comparison=comparison,
            effects={
                "side_effect_free": True,
                "provider_calls": 0,
                "legacy_writes": 0,
                "live_state_writes": 0,
            },
        )

    def _compile_and_authorize(
        self,
        action: Mapping[str, Any],
        *,
        created_at: str | None = None,
    ) -> tuple[CompiledJob, tuple[Mapping[str, str], ...], Any]:
        compiled, compiler_denials = self.compile_action(action, created_at=created_at)
        return compiled, compiler_denials, self.registry.authorize(
            compiled.spec.capability_grants,
            forbidden=compiled.spec.forbidden_capabilities,
        )

    def compile_action(
        self,
        action: Mapping[str, Any],
        *,
        created_at: str | None = None,
    ) -> tuple[CompiledJob, tuple[Mapping[str, str], ...]]:
        metadata = action.get("metadata") if isinstance(action.get("metadata"), Mapping) else {}
        route = metadata.get("route") if isinstance(metadata.get("route"), Mapping) else {}
        operation = metadata.get("proposed_operation") if isinstance(metadata.get("proposed_operation"), Mapping) else {}
        legacy_capability = str(operation.get("capability") or route.get("capability") or "none")
        mapped = CAPABILITY_MAP.get(legacy_capability)
        denials: list[Mapping[str, str]] = []
        grants = []
        target = str(operation.get("target") or route.get("target") or "").strip()
        if mapped:
            capability, mode = mapped
            if capability in EXACT_TARGET_REQUIRED and not target:
                denials.append({"code": "missing_exact_target", "capability": capability, "detail": "External commit has no exact target."})
            approval = capability in EXACT_TARGET_REQUIRED
            grants.append(CapabilityGrant(
                capability=capability,
                version="1",
                mode=mode,
                selectors={"target": target} if target else {},
                constraints={},
                limits={"max_calls": 10},
                approval_required=approval,
                cleanup_required=capability in {"calendar.create"},
            ).to_dict())
        elif legacy_capability == "none":
            denials.append({"code": "capability_clarification_required", "capability": "none", "detail": "No explicit capability could be compiled."})
        else:
            denials.append({"code": "capability_escalation_required", "capability": legacy_capability, "detail": "Legacy route has no v2 capability mapping."})
        origin = "task_card" if action.get("origin") == "task_card" else "global_chat"
        task = action.get("task") if isinstance(action.get("task"), Mapping) else {}
        task_binding = str(task.get("id") or "").strip() if origin == "task_card" else None
        if origin == "task_card" and not task_binding:
            denials.append({"code": "missing_task_binding", "capability": legacy_capability, "detail": "Task-card action has no explicit task ID."})
        resolution_action = action
        if self.context_catalog_provider is not None and not action.get("context_catalog"):
            resolution_action = dict(action)
            resolution_action["context_catalog"] = self.context_catalog_provider(action)
        resolution = resolve_context(
            resolution_action,
            capability=capability if mapped else legacy_capability,
            target=target,
            task_binding=task_binding,
        )
        denials.extend(resolution.denials)
        context_refs = list(resolution.context_refs)
        instruction = str(action.get("instruction") or action.get("note") or "").strip()
        execution_context = {
            "task_binding": task_binding,
            "context_refs": context_refs,
            "capability_grants": grants,
            "deliverables": [f"Execute {legacy_capability} under the compiled contract."],
            "acceptance_criteria": [f"{legacy_capability} has independent evidence."],
            "approval_policy": {"exact_payload_required": any(row.get("approval_required") for row in grants)},
            "resource_limits": {"tool_calls": 10, "timeout_seconds": 600},
            "ambiguity_policy": "block",
            "idempotency_key": str(action.get("id") or "action-intake"),
        }
        ui_context = {
            "action_id": action.get("id"),
            "kind": action.get("kind"),
            "origin": action.get("origin"),
            "ambient_metadata": dict(metadata),
            "context_resolution": resolution.to_dict(),
        }
        request = {
            "job_id": f"job-{action.get('id') or 'action-intake'}",
            "instruction": instruction or "Clarify the requested CMD action.",
            "origin": origin,
            "execution_context": execution_context,
            "ui_context": ui_context,
        }
        if created_at is not None:
            request["created_at"] = created_at
        compiled = compile_job(request)
        return compiled, tuple(denials)


def _policy_denial_dict(item: PolicyDenial) -> dict[str, str]:
    return {"code": item.code, "capability": item.capability, "detail": item.detail}


def _action_hash(action: Mapping[str, Any]) -> str:
    payload = json.dumps(dict(action), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _replay_created_at(action: Mapping[str, Any]) -> str:
    value = action.get("time")
    return value.strip() if isinstance(value, str) and value.strip() else REPLAY_FALLBACK_CREATED_AT


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))
