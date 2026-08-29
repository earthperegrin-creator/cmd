"""Daemon-side capability broker enforcing every leased call."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .capabilities import CapabilityRegistry
from .jobspec import CapabilityGrant
from .leases import LeaseError, LeaseSigner, VerifiedLease


ProviderExecutor = Callable[[Mapping[str, Any], str], Any]


class BrokerDenied(PermissionError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class BrokerAudit:
    lease_id: str
    capability: str
    status: str
    code: str
    payload_hash: str


def canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    material = {key: value for key, value in payload.items() if key != "approved_payload_hash"}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CapabilityBroker:
    def __init__(
        self,
        signer: LeaseSigner,
        registry: CapabilityRegistry,
        executors: Mapping[str, ProviderExecutor] | None = None,
    ):
        self.signer = signer
        self.registry = registry
        self.executors = dict(executors or {})
        self.audit: list[BrokerAudit] = []
        self._calls: dict[str, int] = {}
        self._consumed_commits: set[tuple[str, str, str]] = set()
        self._derived_handles: dict[tuple[str, str], set[str]] = {}

    def proxy(self, token: str, *, now: datetime | None = None) -> "JobToolProxy":
        lease = self._verify(token, now=now)
        return JobToolProxy(self, token, tuple(sorted({grant.capability for grant in lease.grants})))

    def call(self, token: str, capability: str, payload: Mapping[str, Any], *, now: datetime | None = None) -> Any:
        lease = self._verify(token, now=now)
        payload_hash = canonical_payload_hash(payload) if isinstance(payload, dict) else "invalid"
        try:
            grant = self._grant_for(lease, capability)
            if self._calls.get(lease.lease_id, 0) >= lease.call_budget:
                raise BrokerDenied("call_budget_exhausted", "Lease call budget is exhausted.")
            schema_errors = self.registry.validate_payload(capability, payload)
            if schema_errors:
                raise BrokerDenied("invalid_payload", "; ".join(schema_errors))
            self._enforce_constraints(lease, grant, payload, payload_hash)
            operation_id = str(grant.constraints.get("operation_id", "default"))
            replay_key = (lease.lease_id, capability, operation_id)
            max_calls = int(grant.constraints.get("max_calls", 1 if grant.mode == "commit" else lease.call_budget))
            capability_calls = sum(1 for row in self.audit if row.lease_id == lease.lease_id and row.capability == capability and row.status == "completed")
            if capability_calls >= max_calls:
                code = "commit_lease_replayed" if grant.mode == "commit" else "capability_call_limit"
                raise BrokerDenied(code, "Capability call limit is exhausted.")
            if grant.mode == "commit" and replay_key in self._consumed_commits:
                raise BrokerDenied("commit_lease_replayed", "Commit lease operation was already consumed.")
            executor = self.executors.get(capability)
            if executor is None:
                raise BrokerDenied("executor_unavailable", "No daemon-side executor is registered.")
            self._calls[lease.lease_id] = self._calls.get(lease.lease_id, 0) + 1
            result = executor(payload, f"{lease.spec_hash}:{operation_id}:{payload_hash}")
            if grant.mode == "commit":
                self._consumed_commits.add(replay_key)
            result = self._register_derived_handles(lease, result)
            self.audit.append(BrokerAudit(lease.lease_id, capability, "completed", "ok", payload_hash))
            return result
        except BrokerDenied as error:
            self.audit.append(BrokerAudit(lease.lease_id, capability, "denied", error.code, payload_hash))
            raise

    def _verify(self, token: str, *, now: datetime | None) -> VerifiedLease:
        try:
            return self.signer.verify(token, now=now)
        except LeaseError as error:
            raise BrokerDenied("invalid_lease", str(error)) from error

    @staticmethod
    def _grant_for(lease: VerifiedLease, capability: str) -> CapabilityGrant:
        grant = next((item for item in lease.grants if item.capability == capability), None)
        if grant is None:
            raise BrokerDenied("capability_not_leased", "Capability is absent from the lease.")
        return grant

    def _enforce_constraints(
        self,
        lease: VerifiedLease,
        grant: CapabilityGrant,
        payload: Mapping[str, Any],
        payload_hash: str,
    ) -> None:
        constraints = grant.constraints
        expected_hash = constraints.get("approved_payload_hash")
        supplied_hash = payload.get("approved_payload_hash")
        if expected_hash is not None and (supplied_hash != expected_hash or payload_hash != expected_hash):
            raise BrokerDenied("approved_payload_mismatch", "Payload differs from the approved preview.")
        allowed_recipients = set(constraints.get("allowed_recipients", ()))
        recipients = {
            address.lower()
            for field in ("to", "cc", "bcc", "attendees")
            for address in payload.get(field, [])
            if isinstance(address, str)
        }
        if allowed_recipients and not recipients <= {item.lower() for item in allowed_recipients}:
            raise BrokerDenied("recipient_out_of_scope", "A recipient or attendee is outside the lease.")
        self._check_scalar(payload, "calendar_id", constraints.get("allowed_calendar_ids"), "calendar_out_of_scope")
        self._check_scalar(payload, "parent_id", constraints.get("allowed_drive_parent_ids"), "drive_parent_out_of_scope")
        allowed_paths = constraints.get("allowed_paths")
        if allowed_paths:
            roots = tuple(Path(item).expanduser().resolve() for item in allowed_paths)
            for field in ("path", "local_path"):
                if field in payload:
                    target = Path(str(payload[field])).expanduser().resolve()
                    if not any(target == root or root in target.parents for root in roots):
                        raise BrokerDenied("path_out_of_scope", f"{field} escapes allowed paths.")
        allowed_resources = set(constraints.get("allowed_resource_ids", ()))
        resource_fields = constraints.get("resource_fields", ("message_id", "file_id", "event_id", "draft_id", "task_id", "source_id", "docx_file_id"))
        if allowed_resources:
            for field in resource_fields:
                if field in payload and payload[field] not in allowed_resources:
                    raise BrokerDenied("resource_out_of_scope", f"{field} is outside the lease.")
        derived_from = constraints.get("derived_from")
        derived_field = constraints.get("derived_resource_field")
        if derived_from and derived_field:
            handles = self._derived_handles.get((lease.lease_id, str(derived_from)), set())
            if payload.get(str(derived_field)) not in handles:
                raise BrokerDenied("derived_handle_required", "Resource was not derived from the authorized search.")

    @staticmethod
    def _check_scalar(payload: Mapping[str, Any], field: str, allowed: Any, code: str) -> None:
        if allowed and payload.get(field) not in set(allowed):
            raise BrokerDenied(code, f"{field} is outside the lease.")

    def _register_derived_handles(self, lease: VerifiedLease, result: Any) -> Any:
        if not isinstance(result, dict) or "_derived_handles" not in result:
            return result
        public = dict(result)
        handles = public.pop("_derived_handles")
        if not isinstance(handles, dict):
            raise BrokerDenied("invalid_derived_handles", "Executor returned malformed derived handles.")
        for capability, values in handles.items():
            if isinstance(values, list) and all(isinstance(value, str) for value in values):
                self._derived_handles.setdefault((lease.lease_id, capability), set()).update(values)
            else:
                raise BrokerDenied("invalid_derived_handles", "Executor returned malformed derived handles.")
        return public


@dataclass(frozen=True)
class JobToolProxy:
    broker: CapabilityBroker
    token: str
    tools: tuple[str, ...]

    def call(self, capability: str, payload: Mapping[str, Any], *, now: datetime | None = None) -> Any:
        if capability not in self.tools:
            raise BrokerDenied("capability_not_leased", "Tool is not exposed by this job proxy.")
        return self.broker.call(self.token, capability, payload, now=now)
