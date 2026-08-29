"""Immutable JobSpec contract and execution-context firewall."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


ORIGINS = frozenset({"global_chat", "task_card", "automation", "test"})
AMBIGUITY_POLICIES = frozenset({"block", "use_declared_defaults"})
GRANT_MODES = frozenset({"read", "prepare", "commit"})
EXECUTION_CONTEXT_FIELDS = frozenset({
    "task_binding",
    "context_refs",
    "capability_grants",
    "forbidden_capabilities",
    "deliverables",
    "acceptance_criteria",
    "approval_policy",
    "resource_limits",
    "ambiguity_policy",
    "idempotency_key",
    "test_run",
})
SPEC_FIELDS = frozenset({
    "schema_version",
    "job_id",
    "created_at",
    "raw_instruction",
    "origin",
    "outcome",
    "task_binding",
    "context_refs",
    "capability_grants",
    "forbidden_capabilities",
    "deliverables",
    "acceptance_criteria",
    "approval_policy",
    "resource_limits",
    "ambiguity_policy",
    "idempotency_key",
    "test_run",
    "previous_spec_hash",
})


class JobSpecError(ValueError):
    """Raised when a request cannot become a valid execution contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise JobSpecError(f"unknown {label} field(s): {', '.join(unknown)}")


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise JobSpecError(f"{label} must be a non-empty string")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise JobSpecError(f"{label} must be an array")
    return tuple(_string(item, f"{label} item") for item in value)


def _frozen_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise JobSpecError(f"{label} must be an object")
    cloned = json.loads(json.dumps(value, sort_keys=True))
    return _freeze(cloned)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class ContextRef:
    ref_id: str
    kind: str
    provenance: str
    locator: str
    sha256: str | None = None

    @classmethod
    def from_dict(cls, value: Any) -> "ContextRef":
        if not isinstance(value, dict):
            raise JobSpecError("context reference must be an object")
        _require_keys(value, frozenset({"ref_id", "kind", "provenance", "locator", "sha256"}), "context reference")
        digest = value.get("sha256")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise JobSpecError("context reference sha256 must be a lowercase SHA-256 digest")
        return cls(
            ref_id=_string(value.get("ref_id"), "context reference ref_id"),
            kind=_string(value.get("kind"), "context reference kind"),
            provenance=_string(value.get("provenance"), "context reference provenance"),
            locator=_string(value.get("locator"), "context reference locator"),
            sha256=digest,
        )


@dataclass(frozen=True)
class CapabilityGrant:
    capability: str
    version: str
    mode: str
    selectors: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    constraints: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    limits: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    expires_at: str | None = None
    approval_required: bool = False
    verification_required: bool = True
    cleanup_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "version": self.version,
            "mode": self.mode,
            "selectors": _thaw(self.selectors),
            "constraints": _thaw(self.constraints),
            "limits": _thaw(self.limits),
            "expires_at": self.expires_at,
            "approval_required": self.approval_required,
            "verification_required": self.verification_required,
            "cleanup_required": self.cleanup_required,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CapabilityGrant":
        if not isinstance(value, dict):
            raise JobSpecError("capability grant must be an object")
        allowed = frozenset({
            "capability", "version", "mode", "selectors", "constraints", "limits",
            "expires_at", "approval_required", "verification_required", "cleanup_required",
        })
        _require_keys(value, allowed, "capability grant")
        mode = _string(value.get("mode"), "capability grant mode")
        if mode not in GRANT_MODES:
            raise JobSpecError(f"unsupported capability grant mode: {mode}")
        for key in ("approval_required", "verification_required", "cleanup_required"):
            if key in value and not isinstance(value[key], bool):
                raise JobSpecError(f"capability grant {key} must be boolean")
        expires_at = value.get("expires_at")
        if expires_at is not None:
            _string(expires_at, "capability grant expires_at")
        return cls(
            capability=_string(value.get("capability"), "capability grant capability"),
            version=_string(value.get("version"), "capability grant version"),
            mode=mode,
            selectors=_frozen_mapping(value.get("selectors", {}), "capability grant selectors"),
            constraints=_frozen_mapping(value.get("constraints", {}), "capability grant constraints"),
            limits=_frozen_mapping(value.get("limits", {}), "capability grant limits"),
            expires_at=expires_at,
            approval_required=value.get("approval_required", False),
            verification_required=value.get("verification_required", True),
            cleanup_required=value.get("cleanup_required", False),
        )


@dataclass(frozen=True)
class JobSpec:
    schema_version: str
    job_id: str
    created_at: str
    raw_instruction: str
    origin: str
    outcome: str
    task_binding: str | None
    context_refs: tuple[ContextRef, ...]
    capability_grants: tuple[CapabilityGrant, ...]
    deliverables: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    approval_policy: Mapping[str, Any]
    resource_limits: Mapping[str, Any]
    ambiguity_policy: str
    idempotency_key: str
    test_run: Mapping[str, Any] | None
    previous_spec_hash: str | None = None
    forbidden_capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "raw_instruction": self.raw_instruction,
            "origin": self.origin,
            "outcome": self.outcome,
            "task_binding": self.task_binding,
            "context_refs": [
                {
                    "ref_id": ref.ref_id,
                    "kind": ref.kind,
                    "provenance": ref.provenance,
                    "locator": ref.locator,
                    "sha256": ref.sha256,
                }
                for ref in self.context_refs
            ],
            "capability_grants": [grant.to_dict() for grant in self.capability_grants],
            "forbidden_capabilities": list(self.forbidden_capabilities),
            "deliverables": list(self.deliverables),
            "acceptance_criteria": list(self.acceptance_criteria),
            "approval_policy": _thaw(self.approval_policy),
            "resource_limits": _thaw(self.resource_limits),
            "ambiguity_policy": self.ambiguity_policy,
            "idempotency_key": self.idempotency_key,
            "test_run": _thaw(self.test_run) if self.test_run is not None else None,
            "previous_spec_hash": self.previous_spec_hash,
        }

    @property
    def spec_hash(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CompiledJob:
    spec: JobSpec
    ui_context: Mapping[str, Any]


def parse_job_spec(value: Mapping[str, Any]) -> JobSpec:
    if not isinstance(value, dict):
        raise JobSpecError("JobSpec must be an object")
    _require_keys(value, SPEC_FIELDS, "JobSpec")
    missing = sorted(SPEC_FIELDS - {"previous_spec_hash", "forbidden_capabilities"} - set(value))
    if missing:
        raise JobSpecError(f"missing JobSpec field(s): {', '.join(missing)}")
    if value["schema_version"] != "1":
        raise JobSpecError("unsupported JobSpec schema_version")
    origin = _string(value["origin"], "origin")
    if origin not in ORIGINS:
        raise JobSpecError(f"unsupported origin: {origin}")
    ambiguity_policy = _string(value["ambiguity_policy"], "ambiguity_policy")
    if ambiguity_policy not in AMBIGUITY_POLICIES:
        raise JobSpecError(f"unsupported ambiguity_policy: {ambiguity_policy}")
    task_binding = value["task_binding"]
    if task_binding is not None:
        task_binding = _string(task_binding, "task_binding")
    if origin == "global_chat" and task_binding is not None:
        raise JobSpecError("global_chat JobSpec cannot bind a task")
    if origin == "task_card" and task_binding is None:
        raise JobSpecError("task_card JobSpec requires an explicit task_binding")
    test_run = value["test_run"]
    if test_run is not None:
        test_run = _frozen_mapping(test_run, "test_run")
    previous_hash = value.get("previous_spec_hash")
    if previous_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", _string(previous_hash, "previous_spec_hash")):
        raise JobSpecError("previous_spec_hash must be a lowercase SHA-256 digest")
    job_id = _string(value["job_id"], "job_id")
    if not re.fullmatch(r"job-[A-Za-z0-9._-]+", job_id):
        raise JobSpecError("job_id must be path-safe and start with 'job-'")
    context_refs = tuple(ContextRef.from_dict(item) for item in _array(value["context_refs"], "context_refs"))
    ref_ids = [ref.ref_id for ref in context_refs]
    if len(ref_ids) != len(set(ref_ids)):
        raise JobSpecError("context reference ref_id values must be unique")
    grants = tuple(
        CapabilityGrant.from_dict(item)
        for item in _array(value["capability_grants"], "capability_grants")
    )
    forbidden = _string_tuple(value.get("forbidden_capabilities", []), "forbidden_capabilities")
    if len(forbidden) != len(set(forbidden)):
        raise JobSpecError("forbidden_capabilities values must be unique")
    overlap = set(forbidden) & {grant.capability for grant in grants}
    if overlap:
        raise JobSpecError(
            f"capabilities cannot be both granted and forbidden: {', '.join(sorted(overlap))}"
        )
    return JobSpec(
        schema_version="1",
        job_id=job_id,
        created_at=_string(value["created_at"], "created_at"),
        raw_instruction=_string(value["raw_instruction"], "raw_instruction"),
        origin=origin,
        outcome=_string(value["outcome"], "outcome"),
        task_binding=task_binding,
        context_refs=context_refs,
        capability_grants=grants,
        forbidden_capabilities=forbidden,
        deliverables=_string_tuple(value["deliverables"], "deliverables"),
        acceptance_criteria=_string_tuple(value["acceptance_criteria"], "acceptance_criteria"),
        approval_policy=_frozen_mapping(value["approval_policy"], "approval_policy"),
        resource_limits=_frozen_mapping(value["resource_limits"], "resource_limits"),
        ambiguity_policy=ambiguity_policy,
        idempotency_key=_string(value["idempotency_key"], "idempotency_key"),
        test_run=test_run,
        previous_spec_hash=previous_hash,
    )


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise JobSpecError(f"{label} must be an array")
    return value


def compile_job(request: Mapping[str, Any]) -> CompiledJob:
    if not isinstance(request, dict):
        raise JobSpecError("request must be an object")
    raw_instruction = request.get("raw_instruction", request.get("instruction"))
    origin = _string(request.get("origin"), "origin")
    if origin not in ORIGINS:
        raise JobSpecError(f"unsupported origin: {origin}")
    execution = request.get("execution_context", {})
    if not isinstance(execution, dict):
        raise JobSpecError("execution_context must be an object")
    _require_keys(execution, EXECUTION_CONTEXT_FIELDS, "execution_context")
    task_binding = execution.get("task_binding")
    if origin == "global_chat":
        task_binding = None
    spec_value = {
        "schema_version": "1",
        "job_id": request.get("job_id") or f"job-{uuid.uuid4().hex}",
        "created_at": request.get("created_at") or _utc_now(),
        "raw_instruction": raw_instruction,
        "origin": origin,
        "outcome": request.get("outcome") or raw_instruction,
        "task_binding": task_binding,
        "context_refs": execution.get("context_refs", []),
        "capability_grants": execution.get("capability_grants", []),
        "forbidden_capabilities": execution.get("forbidden_capabilities", []),
        "deliverables": execution.get("deliverables", []),
        "acceptance_criteria": execution.get("acceptance_criteria", []),
        "approval_policy": execution.get("approval_policy", {}),
        "resource_limits": execution.get("resource_limits", {}),
        "ambiguity_policy": execution.get("ambiguity_policy", "block"),
        "idempotency_key": execution.get("idempotency_key") or request.get("job_id") or f"request-{uuid.uuid4().hex}",
        "test_run": execution.get("test_run"),
        "previous_spec_hash": request.get("previous_spec_hash"),
    }
    ui_context = _frozen_mapping(request.get("ui_context", request.get("metadata", {})), "ui_context")
    return CompiledJob(spec=parse_job_spec(spec_value), ui_context=ui_context)


def materialize_job(
    compiled: CompiledJob,
    jobs_root: Path,
    source_material: Mapping[str, bytes | str] | None = None,
) -> Path:
    job_dir = jobs_root / compiled.spec.job_id
    if job_dir.exists():
        raise JobSpecError(f"job directory already exists: {job_dir}")
    material = source_material or {}
    allowed_ids = {ref.ref_id for ref in compiled.spec.context_refs}
    extra = sorted(set(material) - allowed_ids)
    if extra:
        raise JobSpecError(f"source material lacks an authorized context reference: {', '.join(extra)}")
    prepared_material: list[tuple[ContextRef, bytes, str]] = []
    for ref in compiled.spec.context_refs:
        if ref.ref_id not in material:
            continue
        payload = material[ref.ref_id]
        if not isinstance(payload, (bytes, str)):
            raise JobSpecError(f"context payload must be bytes or text for {ref.ref_id}")
        content = payload.encode("utf-8") if isinstance(payload, str) else payload
        digest = hashlib.sha256(content).hexdigest()
        if ref.sha256 is not None and ref.sha256 != digest:
            raise JobSpecError(f"context hash mismatch for {ref.ref_id}")
        prepared_material.append((ref, content, digest))
    context_dir = job_dir / "context"
    scratch_dir = job_dir / "scratch"
    artifacts_dir = job_dir / "artifacts"
    for path in (context_dir, scratch_dir, artifacts_dir):
        path.mkdir(parents=True, exist_ok=False)
    (job_dir / "events.jsonl").touch()
    packet = compiled.spec.to_dict()
    packet["spec_hash"] = compiled.spec.spec_hash
    (job_dir / "job.json").write_text(
        json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest = []
    for index, (ref, content, digest) in enumerate(prepared_material):
        filename = f"{index:03d}-{_safe_name(ref.ref_id)}.data"
        (context_dir / filename).write_bytes(content)
        manifest.append({"ref_id": ref.ref_id, "file": filename, "sha256": digest})
    (context_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return job_dir


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-") or "context"
