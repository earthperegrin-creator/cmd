"""Pure context identity resolution for CMD action preflight.

This module deliberately does not read files, query providers, mutate state, or
start workers. Callers provide the local context catalog that should be used
for one resolution. The result is safe to embed in a JobSpec preflight record.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


RESOLUTION_STATUSES = frozenset({"not_requested", "resolved", "missing", "ambiguous", "conflict"})
EXACT_TARGET_CAPABILITIES = frozenset({"gmail.send", "calendar.create"})
CONTEXT_KINDS = frozenset({"company", "person", "source", "policy", "project", "task"})
KIND_ALIASES = {
    "company_name": "company",
    "company_id": "company",
    "person_name": "person",
    "person_id": "person",
    "source_name": "source",
    "source_file": "source",
    "policy_name": "policy",
    "project_name": "project",
}
STOP_WORDS = frozenset({"a", "an", "and", "at", "for", "from", "in", "last", "of", "on", "the", "to", "with"})


@dataclass(frozen=True)
class ContextResolution:
    """The complete side-effect-free result of resolving one action's context."""

    status: str
    context_refs: tuple[Mapping[str, Any], ...]
    checks: tuple[Mapping[str, Any], ...]
    questions: tuple[str, ...]
    denials: tuple[Mapping[str, str], ...]

    def __post_init__(self) -> None:
        if self.status not in RESOLUTION_STATUSES:
            raise ValueError(f"unsupported context resolution status: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "context_refs": _json_clone(self.context_refs),
            "checks": _json_clone(self.checks),
            "questions": list(self.questions),
            "denials": _json_clone(self.denials),
        }


@dataclass(frozen=True)
class _Candidate:
    ref_id: str
    kind: str
    name: str
    locator: str
    provenance: str
    sha256: str | None
    aliases: tuple[str, ...]
    email: str
    domain: str
    company: str
    search: str

    def ref(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "kind": self.kind,
            "provenance": self.provenance,
            "locator": self.locator,
            "sha256": self.sha256,
        }


def resolve_context(
    action: Mapping[str, Any],
    *,
    capability: str = "",
    target: str = "",
    task_binding: str | None = None,
) -> ContextResolution:
    """Resolve explicit action context using only context supplied by the caller.

    Supported action inputs are intentionally small and serializable:

    - ``context_catalog`` at action, metadata, or task level: a list of records;
    - ``context`` at action, metadata, or task level: named declarations such as
      ``{"company": "Acme", "person": "Alex"}``; and
    - ``metadata.proposed_operation.target_ref`` for an exact declared record.

    A missing catalog is itself safe: direct email addresses can still become
    exact recipient references, while names for external commits are denied.
    """

    metadata = _mapping(action.get("metadata"))
    task = _mapping(action.get("task"))
    operation = _mapping(metadata.get("proposed_operation"))
    catalog_value = _first_present(
        action.get("context_catalog"),
        metadata.get("context_catalog"),
        task.get("context_catalog"),
    )
    candidates, catalog_conflicts = _catalog(catalog_value)
    by_ref = {candidate.ref_id: candidate for candidate in candidates}
    refs: dict[str, Mapping[str, Any]] = {}
    checks: list[Mapping[str, Any]] = []
    questions: list[str] = []
    denials: list[Mapping[str, str]] = []
    statuses: list[str] = []

    for conflict in catalog_conflicts:
        statuses.append("conflict")
        checks.append(conflict)
        denials.append({
            "code": "context_catalog_conflict",
            "capability": capability or "none",
            "detail": str(conflict["detail"]),
        })

    if task_binding and task.get("source"):
        _add_ref(refs, {
            "ref_id": f"task-{task_binding}",
            "kind": "task",
            "provenance": "explicit_task_card",
            "locator": str(task["source"]),
            "sha256": None,
        })
        statuses.append("resolved")
        checks.append({
            "kind": "task",
            "query": str(task["source"]),
            "status": "resolved",
            "evidence": ["explicit_task_card"],
            "ref_id": f"task-{task_binding}",
        })

    declarations = _declarations(action, metadata, task)
    for raw_kind, raw_value in declarations:
        kind = KIND_ALIASES.get(raw_kind, raw_kind)
        if kind not in CONTEXT_KINDS:
            checks.append({"kind": raw_kind, "query": str(raw_value), "status": "ignored", "evidence": ["unsupported_context_kind"]})
            continue
        query, declared_ref = _declaration_value(raw_value)
        if not query and not declared_ref:
            continue
        result = _resolve_declaration(kind, query, declared_ref, candidates, by_ref)
        statuses.append(result["status"])
        checks.append(result["check"])
        if result.get("candidate") is not None:
            _add_ref(refs, result["candidate"].ref())
        if result["status"] == "ambiguous":
            questions.append(f"Which {kind} does '{query}' refer to?")
            denials.append({
                "code": "context_ambiguous",
                "capability": capability or "none",
                "detail": str(result["detail"]),
            })
        elif result["status"] == "missing":
            questions.append(f"Which {kind} should CMD use for '{query}'?")
            denials.append({
                "code": "context_missing",
                "capability": capability or "none",
                "detail": str(result["detail"]),
            })
        elif result["status"] == "conflict":
            denials.append({
                "code": "context_conflict",
                "capability": capability or "none",
                "detail": str(result["detail"]),
            })

    target_value = str(target or operation.get("target") or "").strip()
    target_ref = operation.get("target_ref")
    if target_ref is not None or target_value:
        result = _resolve_target(target_value, target_ref, capability, candidates, by_ref)
        statuses.append(result["status"])
        checks.append(result["check"])
        if result.get("candidate") is not None:
            _add_ref(refs, result["candidate"].ref())
        elif result.get("direct_ref") is not None:
            _add_ref(refs, result["direct_ref"])
        if result["status"] in {"ambiguous", "missing", "conflict"} and capability in EXACT_TARGET_CAPABILITIES:
            if result["status"] == "ambiguous":
                questions.append(f"Which exact target does '{target_value}' refer to?")
                code = "context_target_ambiguous"
            elif result["status"] == "conflict":
                code = "context_target_conflict"
            else:
                questions.append(f"What exact target should CMD use for '{target_value}'?")
                code = "context_target_missing"
            denials.append({
                "code": code,
                "capability": capability,
                "detail": str(result["detail"]),
            })

    if not statuses:
        status = "not_requested"
    elif "conflict" in statuses or any(item.get("code", "").endswith("conflict") for item in denials):
        status = "conflict"
    elif "ambiguous" in statuses:
        status = "ambiguous"
    elif "missing" in statuses:
        status = "missing"
    else:
        status = "resolved"
    return ContextResolution(status, tuple(refs.values()), tuple(checks), tuple(dict.fromkeys(questions)), tuple(denials))


def _resolve_declaration(
    kind: str,
    query: str,
    declared_ref: Mapping[str, Any] | None,
    candidates: tuple[_Candidate, ...],
    by_ref: Mapping[str, _Candidate],
) -> dict[str, Any]:
    if declared_ref is not None and declared_ref.get("ref_id"):
        ref_id = str(declared_ref["ref_id"])
        candidate = by_ref.get(ref_id)
        if candidate is None:
            direct = _direct_ref(declared_ref, kind)
            if direct is not None:
                return {"status": "resolved", "candidate": None, "check": {
                    "kind": kind, "query": query, "status": "resolved", "evidence": ["explicit_context_ref"], "ref_id": ref_id,
                }, "direct_ref": direct}
            return {"status": "missing", "candidate": None, "check": {
                "kind": kind, "query": query, "status": "missing", "evidence": ["explicit_ref_not_in_catalog"],
            }, "detail": f"Explicit {kind} reference '{ref_id}' is not present in the context catalog."}
        if query and not _candidate_matches_query(candidate, query):
            return {"status": "conflict", "candidate": None, "check": {
                "kind": kind, "query": query, "status": "conflict", "evidence": ["ref_and_name_disagree"], "ref_id": ref_id,
            }, "detail": f"Explicit {kind} reference '{ref_id}' does not match the declared name '{query}'."}
        return {"status": "resolved", "candidate": candidate, "check": {
            "kind": kind, "query": query, "status": "resolved", "evidence": ["explicit_ref"], "ref_id": candidate.ref_id,
        }}
    return _resolve_query(kind, query, candidates)


def _resolve_target(
    target: str,
    target_ref: Any,
    capability: str,
    candidates: tuple[_Candidate, ...],
    by_ref: Mapping[str, _Candidate],
) -> dict[str, Any]:
    if isinstance(target_ref, Mapping):
        target_kind = str(target_ref.get("kind") or "person").strip().casefold()
        result = _resolve_declaration(target_kind, target, target_ref, candidates, by_ref)
        result["check"] = {**result["check"], "role": "operation_target"}
        return result
    if not target:
        return {
            "status": "missing" if capability in EXACT_TARGET_CAPABILITIES else "not_requested",
            "candidate": None,
            "check": {"kind": "target", "query": "", "status": "missing" if capability in EXACT_TARGET_CAPABILITIES else "not_requested", "evidence": ["no_target"]},
            "detail": "External commit has no target context.",
        }
    if _looks_like_email(target):
        matches = _rank_candidates(target, candidates, allowed_kinds={"person"})
        if len(matches) == 1:
            return {"status": "resolved", "candidate": matches[0][1], "check": {
                "kind": "target", "query": target, "status": "resolved", "evidence": ["catalog_email_exact"], "ref_id": matches[0][1].ref_id,
            }}
        if len(matches) > 1:
            return {"status": "ambiguous", "candidate": None, "check": {
                "kind": "target", "query": target, "status": "ambiguous", "evidence": ["multiple_catalog_matches"], "candidates": [item[1].ref_id for item in matches],
            }, "detail": f"Target email '{target}' matches multiple person records."}
        return {"status": "resolved", "candidate": None, "direct_ref": {
            "ref_id": f"person-email-{_slug(target)}",
            "kind": "person",
            "provenance": "explicit_operation_target",
            "locator": f"email:{target.casefold()}",
            "sha256": None,
        }, "check": {
            "kind": "target", "query": target, "status": "resolved", "evidence": ["explicit_email_exact"],
        }}
    locator_match = re.fullmatch(r"(draft|thread|message|file|calendar|event|task):(.+)", target, re.IGNORECASE)
    if locator_match:
        prefix = locator_match.group(1).casefold()
        kind = "source" if prefix in {"draft", "thread", "message", "file"} else "task"
        return {"status": "resolved", "candidate": None, "direct_ref": {
            "ref_id": f"target-{_slug(target)}",
            "kind": kind,
            "provenance": "explicit_operation_target",
            "locator": target,
            "sha256": None,
        }, "check": {
            "kind": "target", "query": target, "status": "resolved", "evidence": ["explicit_locator_exact"],
        }}
    result = _resolve_query("target", target, candidates)
    if result["status"] == "missing" and capability not in EXACT_TARGET_CAPABILITIES:
        result["check"] = {**result["check"], "evidence": ["no_catalog_match_non_commit"]}
    return result


def _resolve_query(kind: str, query: str, candidates: tuple[_Candidate, ...]) -> dict[str, Any]:
    ranked = _rank_candidates(query, candidates, allowed_kinds=None if kind == "target" else {kind})
    if not ranked:
        return {"status": "missing", "candidate": None, "check": {
            "kind": kind, "query": query, "status": "missing", "evidence": ["no_catalog_match"],
        }, "detail": f"No {kind} context record matches '{query}'."}
    best_score = ranked[0][0]
    best = [candidate for score, candidate in ranked if score == best_score]
    if len(best) != 1:
        return {"status": "ambiguous", "candidate": None, "check": {
            "kind": kind, "query": query, "status": "ambiguous", "evidence": ["multiple_catalog_matches"], "candidates": [candidate.ref_id for candidate in best],
        }, "detail": f"'{query}' matches multiple {kind} context records: {', '.join(candidate.ref_id for candidate in best)}."}
    candidate = best[0]
    evidence = "exact_identity_match" if best_score >= 100 else "all_query_terms_match"
    return {"status": "resolved", "candidate": candidate, "check": {
        "kind": kind, "query": query, "status": "resolved", "evidence": [evidence], "ref_id": candidate.ref_id,
    }}


def _rank_candidates(
    query: str,
    candidates: tuple[_Candidate, ...],
    *,
    allowed_kinds: set[str] | None,
) -> list[tuple[int, _Candidate]]:
    normalized_query = _normalize(query)
    query_terms = _terms(query)
    ranked: list[tuple[int, _Candidate]] = []
    for candidate in candidates:
        if allowed_kinds is not None and candidate.kind not in allowed_kinds:
            continue
        fields = _candidate_fields(candidate)
        normalized_fields = {_normalize(field) for field in fields if field}
        if normalized_query and normalized_query in normalized_fields:
            score = 110 if _looks_like_email(query) and candidate.email and _normalize(candidate.email) == normalized_query else 100
            ranked.append((score, candidate))
            continue
        if query_terms and query_terms <= set(_terms(candidate.search)):
            ranked.append((70 + min(20, len(query_terms) * 5), candidate))
    return sorted(ranked, key=lambda item: (-item[0], item[1].ref_id))


def _catalog(value: Any) -> tuple[tuple[_Candidate, ...], tuple[Mapping[str, Any], ...]]:
    if value is None:
        return (), ()
    if not isinstance(value, list):
        return (), ({"status": "conflict", "detail": "context_catalog must be an array of records."},)
    candidates: list[_Candidate] = []
    conflicts: list[Mapping[str, Any]] = []
    seen: dict[str, _Candidate] = {}
    for index, raw in enumerate(value):
        candidate = _candidate(raw, index)
        if candidate is None:
            conflicts.append({"status": "conflict", "detail": f"context_catalog record {index} is malformed."})
            continue
        previous = seen.get(candidate.ref_id)
        if previous is not None:
            if previous.ref() != candidate.ref():
                conflicts.append({"status": "conflict", "detail": f"context_catalog ref_id '{candidate.ref_id}' has conflicting locators or provenance."})
            continue
        seen[candidate.ref_id] = candidate
        candidates.append(candidate)
    return tuple(candidates), tuple(conflicts)


def _candidate(value: Any, index: int) -> _Candidate | None:
    if not isinstance(value, Mapping):
        return None
    kind = str(value.get("kind") or "").strip().casefold()
    if kind not in CONTEXT_KINDS:
        return None
    name = str(value.get("name") or value.get("title") or value.get("label") or "").strip()
    ref_id = str(value.get("ref_id") or value.get("id") or value.get("slug") or "").strip()
    if not ref_id:
        ref_id = f"{kind}-{_slug(name) or index + 1}"
    locator = str(value.get("locator") or value.get("path") or f"{kind}:{ref_id}").strip()
    provenance = str(value.get("provenance") or "context_catalog").strip()
    aliases = value.get("aliases") if isinstance(value.get("aliases"), list) else []
    alias_values = tuple(str(item).strip() for item in aliases if str(item).strip())
    email = str(value.get("email") or "").strip()
    domain = str(value.get("domain") or "").strip()
    company = str(value.get("company") or value.get("company_name") or "").strip()
    search = " ".join([name, *alias_values, email, domain, company, str(value.get("search") or "")])
    sha256 = value.get("sha256") if isinstance(value.get("sha256"), str) else None
    if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return None
    return _Candidate(
        ref_id=ref_id,
        kind=kind,
        name=name or ref_id,
        locator=locator,
        provenance=provenance,
        sha256=sha256,
        aliases=alias_values,
        email=email,
        domain=domain,
        company=company,
        search=search,
    )


def _declarations(action: Mapping[str, Any], metadata: Mapping[str, Any], task: Mapping[str, Any]) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    for source in (action.get("context"), metadata.get("context"), task.get("context")):
        if not isinstance(source, Mapping):
            continue
        values.extend((str(key).casefold(), value) for key, value in source.items())
    return values


def _declaration_value(value: Any) -> tuple[str, Mapping[str, Any] | None]:
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("query") or "").strip(), value
    return str(value or "").strip(), None


def _direct_ref(value: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    ref_id = str(value.get("ref_id") or value.get("id") or "").strip()
    locator = str(value.get("locator") or value.get("path") or "").strip()
    if not ref_id or not locator:
        return None
    return {
        "ref_id": ref_id,
        "kind": str(value.get("kind") or kind),
        "provenance": str(value.get("provenance") or "explicit_context_ref"),
        "locator": locator,
        "sha256": value.get("sha256") if isinstance(value.get("sha256"), str) else None,
    }


def _candidate_matches_query(candidate: _Candidate, query: str) -> bool:
    return bool(_rank_candidates(query, (candidate,), allowed_kinds={candidate.kind}))


def _candidate_fields(candidate: _Candidate) -> tuple[str, ...]:
    return (candidate.ref_id, candidate.name, *candidate.aliases, candidate.email, candidate.domain, candidate.company, candidate.locator)


def _add_ref(refs: dict[str, Mapping[str, Any]], value: Mapping[str, Any]) -> None:
    ref_id = str(value.get("ref_id") or "").strip()
    if ref_id and ref_id not in refs:
        refs[ref_id] = dict(value)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()))


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[\w@.-]+", value.casefold()))


def _terms(value: str) -> set[str]:
    return {term for term in re.findall(r"[\w@.-]+", value.casefold()) if term not in STOP_WORDS}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")[:80]


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))
