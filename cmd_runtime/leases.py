"""Signed, short-lived capability leases."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .capabilities import CapabilityRegistry
from .jobspec import CapabilityGrant, JobSpec


class LeaseError(ValueError):
    """Raised when a lease is malformed, invalid, or expired."""


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        raise LeaseError("lease times must be timezone-aware")
    return int(value.timestamp())


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True)
class VerifiedLease:
    lease_id: str
    job_id: str
    spec_hash: str
    nonce: str
    issued_at: int
    expires_at: int
    call_budget: int
    test_run: bool
    grants: tuple[CapabilityGrant, ...]


class LeaseSigner:
    def __init__(self, secret: bytes, registry: CapabilityRegistry):
        if len(secret) < 32:
            raise LeaseError("lease signing secret must be at least 32 bytes")
        self._secret = secret
        self.registry = registry

    def issue(
        self,
        spec: JobSpec,
        grants: Iterable[CapabilityGrant],
        *,
        expires_at: datetime,
        call_budget: int,
        now: datetime | None = None,
        nonce: str | None = None,
    ) -> str:
        now = now or datetime.now(timezone.utc)
        grant_values = tuple(grants)
        decision = self.registry.authorize(grant_values, test_run=spec.test_run is not None)
        if not decision.allowed:
            codes = ", ".join(denial.code for denial in decision.denials)
            raise LeaseError(f"grants are not authorized: {codes}")
        for grant in grant_values:
            approved_hash = grant.constraints.get("approved_payload_hash")
            if grant.mode == "commit" and grant.approval_required:
                if not isinstance(approved_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", approved_hash):
                    raise LeaseError("approval-bearing commit grant requires an approved_payload_hash constraint")
        if call_budget < 1:
            raise LeaseError("call_budget must be positive")
        issued = _timestamp(now)
        expiry = _timestamp(expires_at)
        if expiry <= issued:
            raise LeaseError("lease expiry must be after issuance")
        nonce = nonce or secrets.token_urlsafe(18)
        payload = {
            "v": 1,
            "lease_id": f"lease-{secrets.token_hex(12)}",
            "job_id": spec.job_id,
            "spec_hash": spec.spec_hash,
            "nonce": nonce,
            "issued_at": issued,
            "expires_at": expiry,
            "call_budget": call_budget,
            "test_run": spec.test_run is not None,
            "grants": [grant.to_dict() for grant in grant_values],
        }
        encoded = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        signature = _b64encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
        return f"{encoded}.{signature}"

    def verify(self, token: str, *, now: datetime | None = None) -> VerifiedLease:
        try:
            encoded, supplied = token.split(".", 1)
            expected = _b64encode(hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest())
            if not hmac.compare_digest(supplied, expected):
                raise LeaseError("lease signature is invalid")
            payload = json.loads(_b64decode(encoded))
        except LeaseError:
            raise
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise LeaseError("lease token is malformed") from error
        required = {
            "v", "lease_id", "job_id", "spec_hash", "nonce", "issued_at", "expires_at",
            "call_budget", "test_run", "grants",
        }
        if not isinstance(payload, dict) or set(payload) != required or payload.get("v") != 1:
            raise LeaseError("lease payload shape is invalid")
        current = _timestamp(now or datetime.now(timezone.utc))
        if not isinstance(payload["expires_at"], int) or current >= payload["expires_at"]:
            raise LeaseError("lease has expired")
        if not isinstance(payload["issued_at"], int) or current < payload["issued_at"]:
            raise LeaseError("lease is not active yet")
        if not isinstance(payload["call_budget"], int) or payload["call_budget"] < 1:
            raise LeaseError("lease call budget is invalid")
        grants = tuple(CapabilityGrant.from_dict(value) for value in payload["grants"])
        decision = self.registry.authorize(grants, test_run=bool(payload["test_run"]))
        if not decision.allowed:
            raise LeaseError("lease contains unauthorized grants")
        return VerifiedLease(
            lease_id=str(payload["lease_id"]), job_id=str(payload["job_id"]),
            spec_hash=str(payload["spec_hash"]), nonce=str(payload["nonce"]),
            issued_at=payload["issued_at"], expires_at=payload["expires_at"],
            call_budget=payload["call_budget"], test_run=bool(payload["test_run"]), grants=grants,
        )
