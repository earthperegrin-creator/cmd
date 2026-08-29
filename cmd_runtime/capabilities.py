"""Versioned capability registry and deterministic authorization policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .jobspec import CapabilityGrant


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "capabilities" / "v1" / "registry.json"
MANIFEST_FIELDS = frozenset({
    "name", "version", "modes", "risk", "idempotency", "executor", "verifier",
    "cleanup", "test_mode", "input_schema",
})
RISKS = frozenset({
    "read_only", "local_write", "reversible_external", "external_commit",
    "destructive", "sensitive_workflow",
})
IDEMPOTENCY = frozenset({"read", "payload_hash", "provider_key"})


class RegistryError(ValueError):
    """Raised when checked-in capability metadata is invalid."""


@dataclass(frozen=True)
class CapabilityManifest:
    name: str
    version: str
    modes: tuple[str, ...]
    risk: str
    idempotency: str
    executor: str
    verifier: str
    cleanup: str | None
    test_mode: bool
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class PolicyDenial:
    code: str
    capability: str
    detail: str


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    manifests: tuple[CapabilityManifest, ...]
    denials: tuple[PolicyDenial, ...]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(manifest.name for manifest in self.manifests) if self.allowed else ()


class CapabilityRegistry:
    def __init__(self, manifests: Iterable[CapabilityManifest], version: str = "1"):
        self.version = version
        values = tuple(manifests)
        names = [manifest.name for manifest in values]
        if len(names) != len(set(names)):
            raise RegistryError("capability names must be unique")
        self._manifests = {manifest.name: manifest for manifest in values}

    @classmethod
    def load(cls, path: Path = REGISTRY_PATH) -> "CapabilityRegistry":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"registry_version", "capabilities"}:
            raise RegistryError("registry must contain only registry_version and capabilities")
        if value["registry_version"] != "1" or not isinstance(value["capabilities"], list):
            raise RegistryError("unsupported or malformed capability registry")
        return cls((_parse_manifest(item) for item in value["capabilities"]), value["registry_version"])

    def all(self) -> tuple[CapabilityManifest, ...]:
        return tuple(self._manifests[name] for name in sorted(self._manifests))

    def get(self, name: str) -> CapabilityManifest | None:
        return self._manifests.get(name)

    def authorize(
        self,
        grants: Iterable[CapabilityGrant],
        *,
        test_run: bool = False,
        forbidden: Iterable[str] = (),
    ) -> PolicyDecision:
        forbidden_set = set(forbidden)
        denials: list[PolicyDenial] = []
        approved: dict[str, tuple[CapabilityManifest, str]] = {}
        for grant in grants:
            manifest = self.get(grant.capability)
            if manifest is None:
                denials.append(PolicyDenial(
                    "capability_escalation_required",
                    grant.capability,
                    "Capability is not present in the active registry.",
                ))
                continue
            if grant.version != manifest.version:
                denials.append(PolicyDenial("version_not_allowed", grant.capability, "Grant version does not match the registry."))
                continue
            if grant.mode not in manifest.modes:
                denials.append(PolicyDenial("mode_not_allowed", grant.capability, f"Mode {grant.mode} is not supported."))
                continue
            if (
                grant.mode == "commit"
                and manifest.risk in {"external_commit", "destructive", "sensitive_workflow"}
                and not grant.approval_required
            ):
                denials.append(PolicyDenial(
                    "human_approval_required",
                    grant.capability,
                    f"{manifest.risk} commits require an approval-bearing grant.",
                ))
                continue
            if grant.capability in forbidden_set:
                denials.append(PolicyDenial("forbidden_capability", grant.capability, "Capability is explicitly forbidden by this job."))
                continue
            if test_run and not manifest.test_mode:
                denials.append(PolicyDenial("test_mode_forbidden", grant.capability, "Capability is disabled for test runs."))
                continue
            fingerprint = json.dumps(grant.to_dict(), sort_keys=True, separators=(",", ":"))
            existing = approved.get(grant.capability)
            if existing is not None and existing[1] != fingerprint:
                denials.append(PolicyDenial("conflicting_grants", grant.capability, "Duplicate grants have different constraints."))
                continue
            approved[grant.capability] = (manifest, fingerprint)
        if denials:
            return PolicyDecision(False, (), tuple(denials))
        manifests = tuple(approved[name][0] for name in sorted(approved))
        return PolicyDecision(True, manifests, ())

    def validate_payload(self, capability: str, payload: Any) -> tuple[str, ...]:
        manifest = self.get(capability)
        if manifest is None:
            return ("capability_escalation_required",)
        errors = list(_validate_schema(payload, manifest.input_schema, "$"))
        if capability in {"buffer.draft", "buffer.schedule", "buffer.publish"} and isinstance(payload, dict):
            errors.extend(_validate_buffer_social_payload(payload))
        return tuple(errors)


def _validate_buffer_social_payload(payload: Mapping[str, Any]) -> Iterable[str]:
    text = payload.get("text")
    thread = payload.get("twitter_thread")
    repost = payload.get("twitter_repost")
    has_linkedin = bool(payload.get("linkedin_mentions") or payload.get("linkedin_link_attachment"))
    has_twitter = bool(thread or repost)
    if thread and repost:
        yield "$.twitter_thread: cannot be combined with twitter_repost"
    if has_linkedin and has_twitter:
        yield "$: LinkedIn and X metadata cannot be combined"
    if isinstance(text, str) and not text.strip() and not repost:
        yield "$.text: empty text is allowed only for a plain X repost"
    if isinstance(thread, list) and thread and isinstance(text, str):
        first = thread[0] if isinstance(thread[0], dict) else {}
        if first.get("text") != text:
            yield "$.twitter_thread[0].text: must exactly match $.text"
    if isinstance(repost, dict):
        post_id = repost.get("post_id")
        if isinstance(post_id, str) and not post_id.isdigit():
            yield "$.twitter_repost.post_id: must be a numeric X post id"


def _parse_manifest(value: Any) -> CapabilityManifest:
    if not isinstance(value, dict):
        raise RegistryError("capability manifest must be an object")
    missing = MANIFEST_FIELDS - set(value)
    unknown = set(value) - MANIFEST_FIELDS
    if missing or unknown:
        raise RegistryError(f"manifest fields invalid; missing={sorted(missing)} unknown={sorted(unknown)}")
    for key in ("name", "version", "risk", "idempotency", "executor", "verifier"):
        if not isinstance(value[key], str) or not value[key]:
            raise RegistryError(f"manifest {key} must be a non-empty string")
    if value["risk"] not in RISKS:
        raise RegistryError(f"unknown risk tier: {value['risk']}")
    if value["idempotency"] not in IDEMPOTENCY:
        raise RegistryError(f"unknown idempotency mode: {value['idempotency']}")
    if not isinstance(value["modes"], list) or not value["modes"] or any(mode not in {"read", "prepare", "commit"} for mode in value["modes"]):
        raise RegistryError("manifest modes are invalid")
    if value["cleanup"] is not None and (not isinstance(value["cleanup"], str) or not value["cleanup"]):
        raise RegistryError("manifest cleanup must be null or a non-empty string")
    if not isinstance(value["test_mode"], bool) or not isinstance(value["input_schema"], dict):
        raise RegistryError("manifest test_mode or input_schema is invalid")
    schema_errors = list(_validate_schema_shape(value["input_schema"], "$schema"))
    if schema_errors:
        raise RegistryError("; ".join(schema_errors))
    return CapabilityManifest(
        name=value["name"], version=value["version"], modes=tuple(value["modes"]),
        risk=value["risk"], idempotency=value["idempotency"], executor=value["executor"],
        verifier=value["verifier"], cleanup=value["cleanup"], test_mode=value["test_mode"],
        input_schema=value["input_schema"],
    )


def _validate_schema_shape(schema: Any, path: str) -> Iterable[str]:
    if not isinstance(schema, dict) or schema.get("type") not in {"object", "string", "array", "integer", "boolean"}:
        yield f"{path}: unsupported schema"
        return
    if schema.get("type") == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list) or not set(required) <= set(properties):
            yield f"{path}: malformed object schema"
        for name, child in properties.items():
            yield from _validate_schema_shape(child, f"{path}.{name}")
    if schema.get("type") == "array" and "items" in schema:
        yield from _validate_schema_shape(schema["items"], f"{path}[]")


def _validate_schema(value: Any, schema: Mapping[str, Any], path: str) -> Iterable[str]:
    expected = schema.get("type")
    valid_type = {
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
        "array": isinstance(value, list),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(expected, False)
    if not valid_type:
        yield f"{path}: expected {expected}"
        return
    if "enum" in schema and value not in schema["enum"]:
        yield f"{path}: value is not allowed"
    if expected == "string" and len(value) < schema.get("minLength", 0):
        yield f"{path}: string is too short"
    if expected == "integer" and value < schema.get("minimum", value):
        yield f"{path}: value is below minimum"
    if expected == "array":
        if len(value) < schema.get("minItems", 0):
            yield f"{path}: array has too few items"
        if "items" in schema:
            for index, item in enumerate(value):
                yield from _validate_schema(item, schema["items"], f"{path}[{index}]")
    if expected == "object":
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                yield f"{path}.{required}: required field is missing"
        if schema.get("additionalProperties") is False:
            for unknown in sorted(set(value) - set(properties)):
                yield f"{path}.{unknown}: unknown field"
        for key, child in properties.items():
            if key in value:
                yield from _validate_schema(value[key], child, f"{path}.{key}")
