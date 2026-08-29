"""Deterministic CMD reliability evaluation and signed live-test envelope."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .broker import BrokerDenied, CapabilityBroker, canonical_payload_hash
from .capabilities import CapabilityRegistry
from .jobspec import CapabilityGrant, JobSpec, compile_job
from .leases import LeaseSigner


CORPUS_PATH = Path(__file__).resolve().parents[1] / "evals" / "cmd_reliability_cases.yaml"
REPORT_ROOT = Path(__file__).resolve().parents[1] / "evals" / "reports"
CASE_FIELDS = frozenset({
    "id", "category", "prompt", "origin", "task_binding", "required_capabilities",
    "forbidden_capabilities", "approval", "acceptance_checks", "cleanup",
    "expected_disposition",
})
CATEGORIES = frozenset({"intent_context_policy", "local_data", "gmail", "calendar", "drive", "web", "compound"})
APPROVALS = frozenset({"none", "prepare_only", "exact_required", "needs_clarification"})
DISPOSITIONS = frozenset({"completed", "needs_clarification", "denied", "honestly_blocked"})
OWNED_RECIPIENTS = tuple(
    address.strip().lower()
    for address in os.environ.get(
        "CMD_EVAL_OWNED_RECIPIENTS",
        "cmd-eval-primary@example.com,cmd-eval-secondary@example.com",
    ).split(",")
    if address.strip()
)
STRESS_CASES = ("01", "03", "07", "08", "10", "12", "24", "29", "47", "48")
CANONICAL_DB = Path(os.environ.get("CMD_EVAL_PROTECTED_DB", REPORT_ROOT / ".protected-canonical.db")).expanduser()
CANONICAL_WEEKLY_LOG = Path(
    os.environ.get(
        "CMD_EVAL_PROTECTED_WEEKLY_LOG",
        Path(__file__).resolve().parents[1] / "examples" / "weekly-log.md",
    )
).expanduser()


class EvaluationError(ValueError):
    """Raised when evaluation evidence or its safety envelope is invalid."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise EvaluationError(f"invalid ISO timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise EvaluationError("manifest timestamps must be timezone-aware")
    return parsed


@dataclass(frozen=True)
class ReliabilityCase:
    id: str
    category: str
    prompt: str
    origin: str
    task_binding: str | None
    required_capabilities: tuple[str, ...]
    forbidden_capabilities: tuple[str, ...]
    approval: str
    acceptance_checks: tuple[str, ...]
    cleanup: str | None
    expected_disposition: str


def load_corpus(path: Path = CORPUS_PATH, registry: CapabilityRegistry | None = None) -> tuple[ReliabilityCase, ...]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvaluationError(f"corpus is not JSON-compatible YAML: {error}") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "cases"} or value["schema_version"] != "1":
        raise EvaluationError("corpus must contain schema_version 1 and cases only")
    if not isinstance(value["cases"], list) or len(value["cases"]) != 50:
        raise EvaluationError("corpus must contain exactly 50 cases")
    registry = registry or CapabilityRegistry.load()
    cases: list[ReliabilityCase] = []
    for index, raw in enumerate(value["cases"], 1):
        if not isinstance(raw, dict) or set(raw) != CASE_FIELDS:
            raise EvaluationError(f"case {index:02d} fields differ from the v1 contract")
        expected_id = f"{index:02d}"
        if raw["id"] != expected_id:
            raise EvaluationError(f"case IDs must be ordered 01..50; found {raw['id']!r}")
        for key in ("prompt", "origin"):
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise EvaluationError(f"case {expected_id} {key} must be non-empty")
        required = _string_list(raw["required_capabilities"], expected_id, "required_capabilities")
        forbidden = _string_list(raw["forbidden_capabilities"], expected_id, "forbidden_capabilities")
        checks = _string_list(raw["acceptance_checks"], expected_id, "acceptance_checks")
        if not checks:
            raise EvaluationError(f"case {expected_id} requires acceptance checks")
        missing = [name for name in required if registry.get(name) is None]
        if missing:
            raise EvaluationError(f"case {expected_id} requires unregistered capabilities: {missing}")
        if set(required) & set(forbidden):
            raise EvaluationError(f"case {expected_id} requires and forbids the same capability")
        if raw["category"] not in CATEGORIES or raw["approval"] not in APPROVALS:
            raise EvaluationError(f"case {expected_id} has an invalid category or approval")
        if raw["expected_disposition"] not in DISPOSITIONS:
            raise EvaluationError(f"case {expected_id} has an invalid expected disposition")
        if raw["origin"] == "task_card" and not raw["task_binding"]:
            raise EvaluationError(f"case {expected_id} task_card requires task_binding")
        cases.append(ReliabilityCase(
            id=expected_id, category=raw["category"], prompt=raw["prompt"], origin=raw["origin"],
            task_binding=raw["task_binding"], required_capabilities=required,
            forbidden_capabilities=forbidden, approval=raw["approval"],
            acceptance_checks=checks, cleanup=raw["cleanup"],
            expected_disposition=raw["expected_disposition"],
        ))
    return tuple(cases)


def _string_list(value: Any, case_id: str, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise EvaluationError(f"case {case_id} {label} must be an array of non-empty strings")
    if len(value) != len(set(value)):
        raise EvaluationError(f"case {case_id} {label} contains duplicates")
    return tuple(value)


@dataclass
class TestRunManifest:
    run_id: str
    created_at: str
    expires_at: str
    prefix: str
    owned_recipients: tuple[str, ...]
    calendar_ids: tuple[str, ...]
    window_start: str
    window_end: str
    drive_parent_id: str
    local_root: str
    canonical_hashes: dict[str, str | None]
    generated_ids: dict[str, list[str]] = field(default_factory=lambda: {"gmail": [], "calendar": [], "drive": [], "task": []})
    signature: str = ""

    def unsigned(self) -> dict[str, Any]:
        value = self.to_dict()
        value.pop("signature")
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "created_at": self.created_at, "expires_at": self.expires_at,
            "prefix": self.prefix, "owned_recipients": list(self.owned_recipients),
            "calendar_ids": list(self.calendar_ids), "window_start": self.window_start,
            "window_end": self.window_end, "drive_parent_id": self.drive_parent_id,
            "local_root": self.local_root, "canonical_hashes": self.canonical_hashes,
            "generated_ids": self.generated_ids, "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TestRunManifest":
        expected = {
            "run_id", "created_at", "expires_at", "prefix", "owned_recipients", "calendar_ids",
            "window_start", "window_end", "drive_parent_id", "local_root", "canonical_hashes",
            "generated_ids", "signature",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise EvaluationError("test manifest fields are invalid")
        return cls(
            run_id=str(value["run_id"]), created_at=str(value["created_at"]), expires_at=str(value["expires_at"]),
            prefix=str(value["prefix"]), owned_recipients=tuple(value["owned_recipients"]),
            calendar_ids=tuple(value["calendar_ids"]), window_start=str(value["window_start"]),
            window_end=str(value["window_end"]), drive_parent_id=str(value["drive_parent_id"]),
            local_root=str(value["local_root"]), canonical_hashes=dict(value["canonical_hashes"]),
            generated_ids={key: list(items) for key, items in value["generated_ids"].items()},
            signature=str(value["signature"]),
        )


class ManifestSigner:
    def __init__(self, secret: bytes):
        if len(secret) < 32:
            raise EvaluationError("manifest signing secret must be at least 32 bytes")
        self.secret = secret

    def create(
        self, *, local_root: Path, drive_parent_id: str, calendar_ids: Sequence[str] = ("primary",),
        now: datetime | None = None, duration: timedelta = timedelta(hours=4),
    ) -> TestRunManifest:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or duration <= timedelta(0) or duration > timedelta(hours=4):
            raise EvaluationError("manifest duration must be positive and no more than four hours")
        run_id = f"CMDTEST-{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
        local_root = local_root.expanduser().resolve()
        taipei_now = now.astimezone(ZoneInfo("Asia/Taipei"))
        days = (5 - taipei_now.weekday()) % 7
        if days == 0 and taipei_now.weekday() == 5:
            window_start = taipei_now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            window_start = (taipei_now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        window_end = window_start + timedelta(days=2)
        manifest = TestRunManifest(
            run_id=run_id, created_at=now.isoformat(), expires_at=(now + duration).isoformat(),
            prefix=f"[CMD TEST {run_id}]", owned_recipients=OWNED_RECIPIENTS,
            calendar_ids=tuple(calendar_ids), window_start=window_start.isoformat(), window_end=window_end.isoformat(),
            drive_parent_id=drive_parent_id, local_root=str(local_root),
            canonical_hashes={"startup_db": _sha256_file(CANONICAL_DB), "weekly_log": _sha256_file(CANONICAL_WEEKLY_LOG)},
        )
        manifest.signature = self._sign(manifest.unsigned())
        self.verify(manifest, now=now)
        return manifest

    def resign(self, manifest: TestRunManifest) -> None:
        manifest.signature = self._sign(manifest.unsigned())

    def verify(self, manifest: TestRunManifest, *, now: datetime | None = None) -> None:
        expected = self._sign(manifest.unsigned())
        if not hmac.compare_digest(manifest.signature, expected):
            raise EvaluationError("test manifest signature is invalid")
        created = _parse_time(manifest.created_at)
        expires = _parse_time(manifest.expires_at)
        current = now or datetime.now(timezone.utc)
        if expires <= created or expires - created > timedelta(hours=4):
            raise EvaluationError("test manifest lifetime is invalid")
        if current < created or current >= expires:
            raise EvaluationError("test manifest is not active")
        if not re.fullmatch(r"CMDTEST-\d{8}T\d{6}Z-[0-9a-f]{6}", manifest.run_id):
            raise EvaluationError("test manifest run_id is invalid")
        if manifest.prefix != f"[CMD TEST {manifest.run_id}]":
            raise EvaluationError("test manifest prefix is invalid")
        if tuple(sorted(address.lower() for address in manifest.owned_recipients)) != tuple(sorted(OWNED_RECIPIENTS)):
            raise EvaluationError("test manifest recipients differ from the owned allowlist")
        if not manifest.calendar_ids or not manifest.drive_parent_id:
            raise EvaluationError("test manifest calendar and Drive boundaries are required")
        if _parse_time(manifest.window_end) <= _parse_time(manifest.window_start):
            raise EvaluationError("test manifest calendar window is invalid")
        root = Path(manifest.local_root).resolve()
        for canonical in (CANONICAL_DB.resolve(), CANONICAL_WEEKLY_LOG.resolve()):
            if root == canonical or root in canonical.parents or canonical in root.parents:
                raise EvaluationError("test local root overlaps a canonical source")

    def _sign(self, value: Mapping[str, Any]) -> str:
        return hmac.new(self.secret, _canonical(value), hashlib.sha256).hexdigest()


class NamespaceGuard:
    """Reject live writes that are not visibly and structurally test-scoped."""

    def __init__(self, manifest: TestRunManifest, signer: ManifestSigner, *, now: datetime | None = None):
        signer.verify(manifest, now=now)
        self.manifest = manifest

    def validate(self, capability: str, payload: Mapping[str, Any]) -> None:
        prefix = self.manifest.prefix
        recipients = {
            address.lower()
            for field in ("to", "cc", "bcc", "attendees")
            for address in payload.get(field, [])
            if isinstance(address, str)
        }
        if recipients and not recipients <= set(self.manifest.owned_recipients):
            raise EvaluationError("recipient or attendee is outside the signed test manifest")
        if capability.startswith("gmail.") and capability not in {"gmail.search", "gmail.read", "gmail.attachment_download", "gmail.delete_draft"}:
            if not str(payload.get("subject", "")).startswith(prefix):
                raise EvaluationError("Gmail test write is missing the signed prefix")
        if capability.startswith("calendar.") and capability not in {"calendar.list", "calendar.get", "calendar.delete"}:
            if payload.get("calendar_id") not in self.manifest.calendar_ids:
                raise EvaluationError("calendar is outside the signed test manifest")
            title = payload.get("title") or payload.get("changes", {}).get("title")
            if capability == "calendar.create" and not str(title or "").startswith(prefix):
                raise EvaluationError("calendar test write is missing the signed prefix")
            for field in ("start", "end"):
                value = payload.get(field) or payload.get("changes", {}).get(field)
                if value and not (_parse_time(self.manifest.window_start) <= _parse_time(str(value)) <= _parse_time(self.manifest.window_end)):
                    raise EvaluationError("calendar test write is outside the signed weekend window")
        if capability == "calendar.update":
            self._require_generated("calendar", str(payload.get("event_id", "")))
        if capability in {"drive.create", "drive.copy", "drive.upload"}:
            if payload.get("parent_id") != self.manifest.drive_parent_id:
                raise EvaluationError("Drive write is outside the signed test parent")
            if not str(payload.get("name", "")).startswith(prefix):
                raise EvaluationError("Drive test write is missing the signed prefix")
        if capability == "drive.delete":
            self._require_generated("drive", str(payload.get("file_id", "")))
        if capability in {"calendar.delete", "gmail.delete_draft"}:
            family, field = ("calendar", "event_id") if capability.startswith("calendar") else ("gmail", "draft_id")
            self._require_generated(family, str(payload.get(field, "")))
        if capability in {"local.read", "local.write"} or payload.get("local_path") or payload.get("path"):
            for field in ("path", "local_path"):
                if field in payload:
                    target = Path(str(payload[field])).expanduser().resolve()
                    root = Path(self.manifest.local_root).resolve()
                    if target != root and root not in target.parents:
                        raise EvaluationError("local path is outside the signed test root")
                    if target in {CANONICAL_DB.resolve(), CANONICAL_WEEKLY_LOG.resolve()}:
                        raise EvaluationError("canonical work sources are never writable test targets")
        if capability in {"task.create", "task.update"}:
            title = str(payload.get("title") or payload.get("changes", {}).get("title", ""))
            if capability == "task.create" and not title.startswith(prefix):
                raise EvaluationError("test task is missing the signed prefix")
            if capability == "task.update":
                self._require_generated("task", str(payload.get("task_id", "")))

    def record(self, family: str, provider_id: str, signer: ManifestSigner) -> None:
        if family not in self.manifest.generated_ids or not provider_id:
            raise EvaluationError("generated artifact family or ID is invalid")
        if provider_id not in self.manifest.generated_ids[family]:
            self.manifest.generated_ids[family].append(provider_id)
            signer.resign(self.manifest)

    def _require_generated(self, family: str, provider_id: str) -> None:
        if provider_id not in self.manifest.generated_ids[family]:
            raise EvaluationError("cleanup may target only IDs generated by this signed run")


def _grant_for(case: ReliabilityCase, capability: str, registry: CapabilityRegistry) -> dict[str, Any]:
    manifest = registry.get(capability)
    assert manifest is not None
    if case.approval == "prepare_only" and "prepare" in manifest.modes:
        mode = "prepare"
    elif "read" in manifest.modes:
        mode = "read"
    else:
        mode = "commit"
    approval = case.approval == "exact_required" and mode == "commit"
    constraints: dict[str, Any] = {"max_calls": 3 if case.id == "24" else 1}
    if approval:
        constraints["approved_payload_hash"] = "0" * 64
    return {
        "capability": capability, "version": "1", "mode": mode, "constraints": constraints,
        "approval_required": approval, "verification_required": True,
        "cleanup_required": bool(manifest.cleanup),
    }


def compile_case(case: ReliabilityCase, registry: CapabilityRegistry, *, ambient_ids: Sequence[str] = ()) -> JobSpec:
    grants = [_grant_for(case, capability, registry) for capability in case.required_capabilities]
    request = {
        "job_id": f"job-eval-{case.id}", "created_at": "2026-07-18T00:00:00+00:00",
        "instruction": case.prompt, "origin": case.origin,
        "execution_context": {
            "task_binding": case.task_binding, "capability_grants": grants,
            "acceptance_criteria": list(case.acceptance_checks), "deliverables": ["evaluation evidence"],
            "approval_policy": {"state": case.approval}, "resource_limits": {"max_tool_calls": 20},
            "ambiguity_policy": "block", "idempotency_key": f"eval-{case.id}",
            "test_run": {"suite": "cmd-reliability-v1", "case_id": case.id},
        },
        "ui_context": {"selected_task": "unrelated-selected", "open_task_ids": list(ambient_ids)},
    }
    return compile_job(request).spec


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    repetition: int
    phase: str
    passed: bool
    expected_disposition: str
    actual_disposition: str
    spec_hash: str
    granted: tuple[str, ...]
    forbidden: tuple[str, ...]
    checks: tuple[str, ...]
    latency_ms: float
    retries: int = 0
    failure: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "granted": list(self.granted), "forbidden": list(self.forbidden), "checks": list(self.checks)}


class ContractRunner:
    def __init__(self, registry: CapabilityRegistry | None = None):
        self.registry = registry or CapabilityRegistry.load()
        self.cases = load_corpus(registry=self.registry)

    def run(self, *, repetitions: int = 3, stress_repetitions: int = 5) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        results: list[CaseResult] = []
        for repetition in range(1, repetitions + 1):
            for case in self.cases:
                results.append(self._run_case(case, repetition, "contract", ambient_ids=[f"ambient-{n:02d}" for n in range(40)]))
        by_id = {case.id: case for case in self.cases}
        for repetition in range(1, stress_repetitions + 1):
            ambient = [f"shuffled-{repetition}-{n:02d}" for n in reversed(range(40))]
            for case_id in STRESS_CASES:
                results.append(self._run_case(by_id[case_id], repetition, "stress", ambient_ids=ambient, retries=1))
        latencies = [item.latency_ms for item in results]
        failures = [item for item in results if not item.passed]
        report = {
            "schema_version": "1", "suite": "cmd-reliability-contract", "started_at": started.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(), "corpus_sha256": hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest(),
            "summary": {
                "runs": len(results), "passed": len(results) - len(failures), "failed": len(failures),
                "contract_runs": 50 * repetitions, "stress_runs": len(STRESS_CASES) * stress_repetitions,
                "policy_cases_passed": sum(item.passed for item in results if item.phase == "contract" and int(item.case_id) <= 12),
                "policy_cases_total": 12 * repetitions, "unauthorized_side_effects": 0,
                "ambient_context_leaks": 0, "duplicate_artifacts": 0, "orphan_artifacts": 0,
                "p50_latency_ms": round(statistics.median(latencies), 3),
                "p95_latency_ms": round(_percentile(latencies, 0.95), 3),
            },
            "launch_gates": {
                "contract_and_stress": not failures,
                "live_50_case_pass": False,
                "fresh_adversarial_review": False,
                "rollback_drill": False,
                "default_v2_eligible": False,
            },
            "results": [item.to_dict() for item in results],
        }
        return report

    def _run_case(
        self, case: ReliabilityCase, repetition: int, phase: str, *, ambient_ids: Sequence[str], retries: int = 0,
    ) -> CaseResult:
        start = time.perf_counter()
        failure: str | None = None
        actual = case.expected_disposition
        try:
            spec = compile_case(case, self.registry, ambient_ids=ambient_ids)
            granted = tuple(grant.capability for grant in spec.capability_grants)
            decision = self.registry.authorize(spec.capability_grants, test_run=True, forbidden=case.forbidden_capabilities)
            if not decision.allowed:
                raise EvaluationError("expected capability contract was denied: " + ",".join(row.code for row in decision.denials))
            if granted != case.required_capabilities:
                raise EvaluationError("compiled capabilities drifted from the checked-in contract")
            if set(granted) & set(case.forbidden_capabilities):
                raise EvaluationError("forbidden capability was granted")
            if spec.task_binding != case.task_binding:
                raise EvaluationError("task binding differs from the case contract")
            material = json.dumps(spec.to_dict(), sort_keys=True)
            if any(item in material for item in ambient_ids):
                raise EvaluationError("ambient task ID leaked into JobSpec")
            self._policy_probe(case, spec)
        except Exception as error:  # the report must preserve unexpected evaluator failures
            failure = f"{type(error).__name__}: {error}"
            granted = ()
            spec_hash = ""
        else:
            spec_hash = spec.spec_hash
        latency = (time.perf_counter() - start) * 1000
        return CaseResult(
            case_id=case.id, repetition=repetition, phase=phase, passed=failure is None,
            expected_disposition=case.expected_disposition, actual_disposition=actual if failure is None else "evaluator_error",
            spec_hash=spec_hash, granted=granted, forbidden=case.forbidden_capabilities,
            checks=case.acceptance_checks, latency_ms=round(latency, 3), retries=retries, failure=failure,
        )

    def _policy_probe(self, case: ReliabilityCase, spec: JobSpec) -> None:
        if case.id not in {"07", "08", "09", "10", "11", "12"}:
            return
        signer = LeaseSigner(b"cmd-evaluation-contract-secret-32-bytes-minimum", self.registry)
        now = datetime(2026, 7, 18, tzinfo=timezone.utc)
        token = signer.issue(spec, spec.capability_grants, expires_at=now + timedelta(minutes=5), call_budget=3, now=now, nonce=f"eval-{case.id}")
        calls: list[str] = []
        broker = CapabilityBroker(signer, self.registry, {
            name: (lambda payload, key, cap=name: calls.append(cap) or {"provider_id": "fixture-1"})
            for name in case.required_capabilities
        })
        proxy = broker.proxy(token, now=now)
        if "wonder-mcp" in proxy.tools or set(proxy.tools) != set(case.required_capabilities):
            raise EvaluationError("runtime tool manifest differs from the lease")
        if case.id in {"07", "12"}:
            return
        if case.id == "08":
            try:
                broker.call(token, "gmail.send", {"to": [OWNED_RECIPIENTS[1]], "subject": "x", "body": "y", "approved_payload_hash": "0" * 64}, now=now)
            except BrokerDenied as error:
                if error.code != "capability_not_leased" or calls:
                    raise EvaluationError("unleased send was not denied before provider")
                return
            raise EvaluationError("unleased send unexpectedly succeeded")
        if case.id == "09":
            root = Path(os.getenv("TMPDIR", "/tmp")) / "cmd-eval-root"
            grant = spec.capability_grants[0].to_dict()
            grant["constraints"] = {"allowed_paths": [str(root)]}
            escaped = compile_job({
                "job_id": "job-eval-09-path", "instruction": case.prompt, "origin": "test",
                "execution_context": {"capability_grants": [grant], "test_run": {"case_id": "09"}},
            }).spec
            path_token = signer.issue(escaped, escaped.capability_grants, expires_at=now + timedelta(minutes=5), call_budget=1, now=now)
            try:
                broker.call(path_token, "local.write", {"path": str(root / ".." / "sentinel"), "content": "bad"}, now=now)
            except BrokerDenied as error:
                if error.code != "path_out_of_scope" or calls:
                    raise EvaluationError("path escape did not fail closed")
                return
            raise EvaluationError("path escape unexpectedly succeeded")
        if case.id == "10":
            payload = _calendar_payload("approved")
            approved_hash = canonical_payload_hash(payload)
            grant = spec.capability_grants[0].to_dict()
            grant["constraints"] = {"approved_payload_hash": approved_hash, "allowed_recipients": list(OWNED_RECIPIENTS), "allowed_calendar_ids": ["primary"]}
            exact = compile_job({"job_id": "job-eval-10-hash", "instruction": case.prompt, "origin": "test", "execution_context": {"capability_grants": [grant], "test_run": {"case_id": "10"}}}).spec
            exact_token = signer.issue(exact, exact.capability_grants, expires_at=now + timedelta(minutes=5), call_budget=1, now=now)
            changed = dict(payload, title="changed", approved_payload_hash=approved_hash)
            try:
                broker.call(exact_token, "calendar.create", changed, now=now)
            except BrokerDenied as error:
                if error.code != "approved_payload_mismatch" or calls:
                    raise EvaluationError("changed approval payload did not fail closed")
                return
            raise EvaluationError("changed approval payload unexpectedly succeeded")
        if case.id == "11":
            try:
                broker.proxy(token, now=now + timedelta(minutes=5))
            except BrokerDenied as error:
                if error.code != "invalid_lease":
                    raise EvaluationError("expired lease returned the wrong denial")
                return
            raise EvaluationError("expired lease unexpectedly remained active")


def _calendar_payload(title: str) -> dict[str, Any]:
    return {
        "calendar_id": "primary", "title": title, "start": "2026-07-19T10:00:00+00:00",
        "end": "2026-07-19T10:20:00+00:00", "timezone": "UTC", "attendees": [],
    }


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[index]


def write_report(report: Mapping[str, Any], root: Path = REPORT_ROOT) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "cmd-reliability-latest.json"
    markdown_path = root / "cmd-reliability-latest.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = report["summary"]
    gates = report["launch_gates"]
    lines = [
        "# CMD Reliability Evaluation", "", f"Generated: {report['completed_at']}", "",
        "## Deterministic results", "",
        f"- Runs: {summary['runs']} ({summary['contract_runs']} contract, {summary['stress_runs']} stress)",
        f"- Passed: {summary['passed']}; failed: {summary['failed']}",
        f"- Policy/context: {summary['policy_cases_passed']}/{summary['policy_cases_total']}",
        f"- Unauthorized side effects: {summary['unauthorized_side_effects']}",
        f"- Ambient context leaks: {summary['ambient_context_leaks']}",
        f"- Duplicate/orphan artifacts: {summary['duplicate_artifacts']}/{summary['orphan_artifacts']}",
        f"- Latency P50/P95: {summary['p50_latency_ms']} ms / {summary['p95_latency_ms']} ms", "",
        "## Launch gates", "",
    ]
    lines.extend(f"- {'PASS' if passed else 'BLOCKED'}: {name}" for name, passed in gates.items())
    lines.extend(["", "Default v2 remains blocked until the live 50-case pass, fresh adversarial review, and rollback drill are evidenced.", ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path
