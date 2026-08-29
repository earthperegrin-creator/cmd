"""Independent deterministic and semantic verification for CMD jobs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .jobspec import JobSpec


TRUSTED_SOURCES = frozenset({"file_readback", "sqlite_readback", "provider_readback", "fresh_semantic_review"})
PRIVATE_ARTIFACT_FIELDS = frozenset({"maker_reasoning", "reasoning", "chain_of_thought", "scratch"})


@dataclass(frozen=True)
class Evidence:
    criterion: str
    passed: bool
    source: str
    detail: str
    data: Mapping[str, Any]


@dataclass(frozen=True)
class VerificationReport:
    status: str
    criteria: tuple[Evidence, ...]
    unmet: tuple[str, ...]


def verify_file(criterion: str, path: Path, *, expected_sha256: str | None = None) -> Evidence:
    if not path.is_file():
        return Evidence(criterion, False, "file_readback", "File does not exist.", {"path": str(path)})
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    passed = expected_sha256 is None or digest == expected_sha256
    return Evidence(
        criterion, passed, "file_readback",
        "File exists with expected hash." if passed else "File hash differs from expected artifact.",
        {"path": str(path), "sha256": digest},
    )


def verify_sqlite_row(
    criterion: str,
    database: Path,
    query: str,
    parameters: tuple[Any, ...] = (),
    *,
    expected_count: int = 1,
) -> Evidence:
    if not database.is_file() or not query.lstrip().lower().startswith("select"):
        return Evidence(criterion, False, "sqlite_readback", "Safe SQLite readback is unavailable.", {})
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.close()
    passed = len(rows) == expected_count
    return Evidence(
        criterion, passed, "sqlite_readback",
        f"SQLite readback returned {len(rows)} row(s); expected {expected_count}.",
        {"row_count": len(rows)},
    )


def verify_provider_artifact(
    criterion: str,
    expected: Mapping[str, Any],
    readback: Mapping[str, Any] | None,
    *,
    material_fields: Iterable[str],
    artifact_count: int = 1,
) -> Evidence:
    if not readback or not readback.get("provider_id"):
        return Evidence(criterion, False, "provider_readback", "Provider artifact readback is missing.", {})
    mismatches = {
        field: {"expected": expected.get(field), "actual": readback.get(field)}
        for field in material_fields
        if expected.get(field) != readback.get(field)
    }
    passed = not mismatches and artifact_count == 1
    detail = "Provider artifact and all material fields match."
    if mismatches:
        detail = f"Provider readback differs in: {', '.join(sorted(mismatches))}."
    elif artifact_count != 1:
        detail = f"Expected one provider artifact; found {artifact_count}."
    return Evidence(
        criterion, passed, "provider_readback", detail,
        {"provider_id": readback["provider_id"], "mismatches": mismatches, "artifact_count": artifact_count},
    )


def semantic_review_packet(
    spec: JobSpec,
    artifacts: Iterable[Mapping[str, Any]],
    deterministic_evidence: Iterable[Evidence],
) -> dict[str, Any]:
    return {
        "job_spec": spec.to_dict(),
        "artifacts": [
            {key: value for key, value in artifact.items() if key not in PRIVATE_ARTIFACT_FIELDS}
            for artifact in artifacts
        ],
        "deterministic_evidence": [
            {"criterion": item.criterion, "passed": item.passed, "source": item.source, "detail": item.detail, "data": dict(item.data)}
            for item in deterministic_evidence
        ],
        "criteria": list(spec.acceptance_criteria),
    }


SemanticReviewer = Callable[[Mapping[str, Any]], Mapping[str, Mapping[str, Any]]]


def run_semantic_review(
    spec: JobSpec,
    artifacts: Iterable[Mapping[str, Any]],
    deterministic_evidence: Iterable[Evidence],
    reviewer: SemanticReviewer,
) -> tuple[Evidence, ...]:
    packet = semantic_review_packet(spec, artifacts, deterministic_evidence)
    response = reviewer(packet)
    results = []
    for criterion in spec.acceptance_criteria:
        value = response.get(criterion, {})
        passed = value.get("passed") is True
        results.append(Evidence(
            criterion, passed, "fresh_semantic_review",
            str(value.get("detail") or "Fresh reviewer did not account for this criterion."),
            {},
        ))
    return tuple(results)


def aggregate_verification(spec: JobSpec, evidence: Iterable[Evidence]) -> VerificationReport:
    if not spec.acceptance_criteria:
        missing = Evidence(
            "acceptance_criteria", False, "missing",
            "JobSpec has no mandatory acceptance criteria.", {},
        )
        return VerificationReport("blocked", (missing,), ("acceptance_criteria",))
    by_criterion: dict[str, list[Evidence]] = {criterion: [] for criterion in spec.acceptance_criteria}
    for item in evidence:
        if item.criterion in by_criterion:
            by_criterion[item.criterion].append(item)
    selected = []
    unmet = []
    for criterion in spec.acceptance_criteria:
        candidates = by_criterion[criterion]
        trusted = [item for item in candidates if item.source in TRUSTED_SOURCES]
        passed = [item for item in trusted if item.passed]
        if passed:
            selected.append(passed[-1])
        else:
            unmet.append(criterion)
            selected.append(trusted[-1] if trusted else Evidence(
                criterion, False, "missing", "No independent evidence was supplied.", {},
            ))
    if not unmet:
        status = "completed"
    elif len(unmet) < len(spec.acceptance_criteria):
        status = "partial"
    else:
        status = "blocked"
    return VerificationReport(status, tuple(selected), tuple(unmet))
