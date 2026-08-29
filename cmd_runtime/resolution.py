"""Registry-first resolution contracts for CMD work and execution planning.

Models may propose a ResolutionPlan, but this module owns the checked-in
vocabulary and deterministic validation. It performs no provider calls and no
state mutation.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from .capabilities import CapabilityRegistry


REGISTRY_ROOT = Path(__file__).resolve().parents[1] / "registries" / "v1"
TOOLS_PATH = REGISTRY_ROOT / "tools.json"
SKILLS_PATH = REGISTRY_ROOT / "skills.json"
WORKFLOWS_PATH = REGISTRY_ROOT / "workflows.json"
LOCAL_OVERLAY_ENV = "CMD_RESOLVER_REGISTRY_OVERLAY_DIR"

ADMISSIONS = frozenset({"no_capture", "capture_only", "dispatch", "clarify"})
OUTCOME_OPERATIONS = frozenset({"no_capture", "attach_existing", "create_new", "clarify"})
PLAN_FIELDS = frozenset({
    "schema_version", "admission", "outcome", "execution", "missing_inputs",
    "approval_gates", "acceptance", "confidence", "reason",
})
OUTCOME_FIELDS = frozenset({"operation", "item_id", "title"})
EXECUTION_FIELDS = frozenset({
    "workflow_id", "skill_ids", "tool_operations", "forbidden_operations",
})
TOOL_FIELDS = frozenset({"id", "name", "description", "operations", "version"})
SKILL_FIELDS = frozenset({
    "id", "name", "aliases", "invocations", "description", "when_to_use",
    "when_not_to_use", "inputs", "outputs", "required_operations",
    "optional_operations", "forbidden_operations", "approval_policy",
    "acceptance", "examples", "counterexamples", "version",
})
WORKFLOW_FIELDS = frozenset({
    "id", "name", "aliases", "description", "steps", "required_operations",
    "optional_operations", "forbidden_operations", "approval_gates",
    "acceptance", "inputs", "outputs", "version",
})
APPROVAL_RISKS = frozenset({"external_commit", "destructive", "sensitive_workflow"})
EXPLICIT_INVOCATION_PATTERN = re.compile(r"(?<![\w-])\$[A-Za-z][A-Za-z0-9_-]*(?![\w-])")


class ResolverRegistryError(ValueError):
    """Raised when checked-in resolver registry data is inconsistent."""


class ResolutionPlanError(ValueError):
    """Raised when a proposed ResolutionPlan is structurally invalid."""


@dataclass(frozen=True)
class ToolManifest:
    id: str
    name: str
    description: str
    operations: tuple[str, ...]
    version: str


@dataclass(frozen=True)
class SkillManifest:
    id: str
    name: str
    aliases: tuple[str, ...]
    invocations: tuple[str, ...]
    description: str
    when_to_use: str
    when_not_to_use: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    required_operations: tuple[str, ...]
    optional_operations: tuple[str, ...]
    forbidden_operations: tuple[str, ...]
    approval_policy: str
    acceptance: tuple[str, ...]
    examples: tuple[str, ...]
    counterexamples: tuple[str, ...]
    version: str

    @property
    def allowed_operations(self) -> frozenset[str]:
        return frozenset((*self.required_operations, *self.optional_operations))


@dataclass(frozen=True)
class WorkflowManifest:
    id: str
    name: str
    aliases: tuple[str, ...]
    description: str
    steps: tuple[str, ...]
    required_operations: tuple[str, ...]
    optional_operations: tuple[str, ...]
    forbidden_operations: tuple[str, ...]
    approval_gates: tuple[str, ...]
    acceptance: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    version: str

    @property
    def allowed_operations(self) -> frozenset[str]:
        return frozenset((*self.required_operations, *self.optional_operations))


@dataclass(frozen=True)
class OutcomeResolution:
    operation: str
    item_id: str | None
    title: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"operation": self.operation, "item_id": self.item_id, "title": self.title}


@dataclass(frozen=True)
class ExecutionResolution:
    workflow_id: str | None
    skill_ids: tuple[str, ...]
    tool_operations: tuple[str, ...]
    forbidden_operations: tuple[str, ...]

    @property
    def empty(self) -> bool:
        return not (
            self.workflow_id
            or self.skill_ids
            or self.tool_operations
            or self.forbidden_operations
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "skill_ids": list(self.skill_ids),
            "tool_operations": list(self.tool_operations),
            "forbidden_operations": list(self.forbidden_operations),
        }


@dataclass(frozen=True)
class ResolutionPlan:
    schema_version: str
    admission: str
    outcome: OutcomeResolution
    execution: ExecutionResolution
    missing_inputs: tuple[str, ...]
    approval_gates: tuple[str, ...]
    acceptance: tuple[str, ...]
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "admission": self.admission,
            "outcome": self.outcome.to_dict(),
            "execution": self.execution.to_dict(),
            "missing_inputs": list(self.missing_inputs),
            "approval_gates": list(self.approval_gates),
            "acceptance": list(self.acceptance),
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ResolutionDenial:
    code: str
    detail: str
    primitive: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "primitive": self.primitive}


@dataclass(frozen=True)
class ResolutionDecision:
    allowed: bool
    plan: ResolutionPlan
    denials: tuple[ResolutionDenial, ...]
    tool_ids: tuple[str, ...]


class ResolverRegistry:
    """Validated product vocabulary layered over atomic capability manifests."""

    def __init__(
        self,
        tools: Iterable[ToolManifest],
        skills: Iterable[SkillManifest],
        workflows: Iterable[WorkflowManifest],
        capability_registry: CapabilityRegistry,
        *,
        version: str = "1",
    ):
        self.version = version
        self.capability_registry = capability_registry
        self._tools = _index_unique(tools, "tool")
        self._skills = _index_unique(skills, "skill")
        self._workflows = _index_unique(workflows, "workflow")
        self._operation_tools: dict[str, ToolManifest] = {}
        self._invocations: dict[str, SkillManifest] = {}
        self._validate()

    @classmethod
    def load(
        cls,
        *,
        tools_path: Path = TOOLS_PATH,
        skills_path: Path = SKILLS_PATH,
        workflows_path: Path = WORKFLOWS_PATH,
        overlay_root: Path | None = None,
        capability_registry: CapabilityRegistry | None = None,
    ) -> "ResolverRegistry":
        tools_payload = _load_registry(tools_path, "tools")
        skills_payload = _load_registry(skills_path, "skills")
        workflows_payload = _load_registry(workflows_path, "workflows")
        versions = {
            tools_payload["registry_version"],
            skills_payload["registry_version"],
            workflows_payload["registry_version"],
        }
        if versions != {"1"}:
            raise ResolverRegistryError("resolver registries must use the same supported version")
        tools = list(tools_payload["tools"])
        skills = list(skills_payload["skills"])
        workflows = list(workflows_payload["workflows"])
        if overlay_root is not None:
            overlay_root = overlay_root.expanduser().resolve()
            for filename, collection, target in (
                ("tools.json", "tools", tools),
                ("skills.json", "skills", skills),
                ("workflows.json", "workflows", workflows),
            ):
                path = overlay_root / filename
                if not path.exists():
                    continue
                overlay = _load_registry(path, collection)
                if overlay["registry_version"] != "1":
                    raise ResolverRegistryError(
                        f"{path} uses an unsupported registry version"
                    )
                target.extend(overlay[collection])
        return cls(
            (_parse_tool(value) for value in tools),
            (_parse_skill(value) for value in skills),
            (_parse_workflow(value) for value in workflows),
            capability_registry or CapabilityRegistry.load(),
            version="1",
        )

    @classmethod
    def load_with_local_overlay(
        cls,
        state_dir: Path,
        *,
        capability_registry: CapabilityRegistry | None = None,
    ) -> "ResolverRegistry":
        configured = os.environ.get(LOCAL_OVERLAY_ENV, "").strip()
        overlay_root = (
            Path(configured).expanduser()
            if configured
            else state_dir.expanduser() / "resolver-registry" / "v1"
        )
        return cls.load(
            overlay_root=overlay_root if overlay_root.is_dir() else None,
            capability_registry=capability_registry,
        )

    def tools(self) -> tuple[ToolManifest, ...]:
        return tuple(self._tools[key] for key in sorted(self._tools))

    def skills(self) -> tuple[SkillManifest, ...]:
        return tuple(self._skills[key] for key in sorted(self._skills))

    def workflows(self) -> tuple[WorkflowManifest, ...]:
        return tuple(self._workflows[key] for key in sorted(self._workflows))

    def tool(self, tool_id: str) -> ToolManifest | None:
        return self._tools.get(tool_id)

    def tool_for_operation(self, operation: str) -> ToolManifest | None:
        return self._operation_tools.get(operation)

    def skill(self, skill_id: str) -> SkillManifest | None:
        return self._skills.get(skill_id)

    def workflow(self, workflow_id: str) -> WorkflowManifest | None:
        return self._workflows.get(workflow_id)

    def skill_for_invocation(self, invocation: str) -> SkillManifest | None:
        return self._invocations.get(invocation.casefold())

    def explicit_skill_candidates(self, text: str) -> tuple[SkillManifest, ...]:
        """Return registered explicit skill invocations in source order."""
        matches: list[tuple[int, SkillManifest]] = []
        unknown: list[str] = []
        for match in EXPLICIT_INVOCATION_PATTERN.finditer(text):
            invocation = match.group(0)
            skill = self.skill_for_invocation(invocation)
            if skill is None:
                unknown.append(invocation)
            else:
                matches.append((match.start(), skill))
        if unknown:
            raise ResolutionPlanError(
                f"unknown explicit skill invocation(s): {', '.join(dict.fromkeys(unknown))}"
            )
        result: list[SkillManifest] = []
        seen: set[str] = set()
        for _, skill in sorted(matches, key=lambda item: (item[0], item[1].id)):
            if skill.id not in seen:
                result.append(skill)
                seen.add(skill.id)
        return tuple(result)

    def lexical_skill_candidates(self, text: str) -> tuple[SkillManifest, ...]:
        """Bound candidate retrieval; this does not make the final selection."""
        explicit = self.explicit_skill_candidates(text)
        if explicit:
            return explicit
        normalized = _normalize_phrase(text)
        matches = []
        for skill in self._skills.values():
            phrases = (skill.name, *skill.aliases)
            if any(_phrase_present(normalized, _normalize_phrase(value)) for value in phrases):
                matches.append(skill)
        return tuple(sorted(matches, key=lambda skill: skill.id))

    def validate_plan(
        self,
        plan: ResolutionPlan | Mapping[str, Any],
        *,
        candidate_outcome_ids: Iterable[str] = (),
        connected_tool_ids: Iterable[str] = (),
        granted_operations: Iterable[str] = (),
        explicit_skill_ids: Iterable[str] = (),
        candidate_skill_ids: Iterable[str] | None = None,
        candidate_workflow_ids: Iterable[str] | None = None,
        confidence_threshold: float = 0.7,
    ) -> ResolutionDecision:
        value = plan if isinstance(plan, ResolutionPlan) else parse_resolution_plan(plan)
        denials: list[ResolutionDenial] = []
        candidates = set(candidate_outcome_ids)
        connected = set(connected_tool_ids)
        granted = set(granted_operations)
        explicit_skills = set(explicit_skill_ids)
        skill_candidates = set(candidate_skill_ids) if candidate_skill_ids is not None else None
        workflow_candidates = set(candidate_workflow_ids) if candidate_workflow_ids is not None else None

        def deny(code: str, detail: str, primitive: str = "") -> None:
            denials.append(ResolutionDenial(code, detail, primitive))

        if value.confidence < confidence_threshold:
            deny("low_confidence", "Resolution confidence is below the configured threshold.")

        operation = value.outcome.operation
        if operation == "attach_existing":
            if not value.outcome.item_id:
                deny("missing_outcome", "Attaching work requires one canonical outcome ID.")
            elif value.outcome.item_id not in candidates:
                deny("outcome_outside_candidates", "The selected outcome is outside the bounded candidate set.", value.outcome.item_id)
            if value.outcome.title:
                deny("conflicting_outcome_fields", "An existing-outcome resolution cannot also propose a new title.")
        elif operation == "create_new":
            if not value.outcome.title:
                deny("missing_outcome_title", "Creating an outcome requires a non-empty title.")
            if value.outcome.item_id:
                deny("conflicting_outcome_fields", "A new-outcome resolution cannot select an existing ID.")
        elif value.outcome.item_id or value.outcome.title:
            deny("unexpected_outcome_fields", "No-capture and clarification resolutions cannot name an outcome effect.")

        expected_operation = {
            "no_capture": "no_capture",
            "clarify": "clarify",
        }.get(value.admission)
        if expected_operation and operation != expected_operation:
            deny("admission_outcome_conflict", f"Admission {value.admission} requires outcome operation {expected_operation}.")
        if value.admission in {"capture_only", "dispatch"} and operation not in {"attach_existing", "create_new"}:
            deny("admission_outcome_conflict", f"Admission {value.admission} requires durable outcome binding.")

        if value.admission in {"no_capture", "clarify", "capture_only"} and not value.execution.empty:
            deny("unexpected_execution", f"Admission {value.admission} cannot carry an execution plan.")
        if value.admission == "clarify" and not value.missing_inputs:
            deny("missing_clarification_input", "A clarification plan must name the missing input.")
        if value.admission != "clarify" and value.missing_inputs:
            deny("unresolved_inputs", "A non-clarification plan cannot retain missing inputs.")

        unknown_explicit = explicit_skills - set(self._skills)
        if unknown_explicit:
            deny("unknown_explicit_skill", "An explicit skill invocation is not registered.", ",".join(sorted(unknown_explicit)))
        execution_explicit = explicit_skills - {"capture-or-update-outcome"}
        if value.admission not in {"dispatch", "clarify"} and execution_explicit:
            deny(
                "explicit_skill_requires_execution",
                "An explicitly invoked execution Skill requires dispatch or clarification.",
                ",".join(sorted(execution_explicit)),
            )

        selected_tools: set[str] = set()
        if value.admission == "dispatch":
            execution = value.execution
            workflow = self.workflow(execution.workflow_id) if execution.workflow_id else None
            if execution.workflow_id and workflow is None:
                deny("unknown_workflow", "The selected workflow is not registered.", execution.workflow_id)
            elif execution.workflow_id and workflow_candidates is not None and execution.workflow_id not in workflow_candidates:
                deny("workflow_outside_candidates", "The selected workflow is outside the supplied resolver candidates.", execution.workflow_id)
            selected_skills: list[SkillManifest] = []
            for skill_id in execution.skill_ids:
                skill = self.skill(skill_id)
                if skill is None:
                    deny("unknown_skill", "The selected skill is not registered.", skill_id)
                else:
                    selected_skills.append(skill)
                    if skill_candidates is not None and skill_id not in skill_candidates:
                        deny("skill_outside_candidates", "The selected skill is outside the supplied resolver candidates.", skill_id)
            omitted_explicit = explicit_skills - set(execution.skill_ids)
            if omitted_explicit:
                deny("explicit_skill_not_selected", "The plan dropped an explicitly invoked skill.", ",".join(sorted(omitted_explicit)))
            if not execution.workflow_id and not selected_skills:
                deny("missing_execution_primitive", "Dispatch requires a registered workflow or at least one skill.")
            if workflow and execution.skill_ids != workflow.steps:
                deny("workflow_steps_changed", "The selected skill sequence must exactly match the registered workflow.", workflow.id)

            required_operations: set[str] = set()
            allowed_operations: set[str] = set()
            required_forbidden: set[str] = set()
            required_acceptance: set[str] = set()
            required_gates: set[str] = set()
            for skill in selected_skills:
                required_operations.update(skill.required_operations)
                allowed_operations.update(skill.allowed_operations)
                required_forbidden.update(skill.forbidden_operations)
                if workflow is None:
                    required_acceptance.update(skill.acceptance)
            if workflow:
                required_operations.update(workflow.required_operations)
                allowed_operations.update(workflow.allowed_operations)
                required_forbidden.update(workflow.forbidden_operations)
                required_acceptance.update(workflow.acceptance)
                required_gates.update(workflow.approval_gates)

            selected_operations = set(execution.tool_operations)
            forbidden_operations = set(execution.forbidden_operations)
            missing_required = required_operations - selected_operations
            extra_operations = selected_operations - allowed_operations
            missing_forbidden = required_forbidden - forbidden_operations
            if missing_required:
                deny("required_operations_missing", "The plan omitted required tool operations.", ",".join(sorted(missing_required)))
            if extra_operations:
                deny("operation_not_allowed_by_skill", "The plan selected operations outside the chosen skill or workflow contracts.", ",".join(sorted(extra_operations)))
            if selected_operations & forbidden_operations:
                deny("forbidden_operation_selected", "A selected operation is also explicitly forbidden.", ",".join(sorted(selected_operations & forbidden_operations)))
            if missing_forbidden:
                deny("forbidden_operations_omitted", "The plan dropped a prohibition declared by the selected skill or workflow.", ",".join(sorted(missing_forbidden)))

            for operation_id in sorted(selected_operations | forbidden_operations):
                if operation_id not in self._operation_tools:
                    deny("unknown_tool_operation", "The plan names an unregistered tool operation.", operation_id)
            for operation_id in sorted(selected_operations):
                tool = self.tool_for_operation(operation_id)
                if tool is None:
                    continue
                selected_tools.add(tool.id)
                if tool.id not in connected:
                    deny("tool_not_connected", "The selected tool is not connected for this user.", tool.id)
                if operation_id not in granted:
                    deny("operation_not_granted", "The selected tool operation is not granted for this job.", operation_id)
                manifest = self.capability_registry.get(operation_id)
                if manifest and manifest.risk in APPROVAL_RISKS and operation_id not in value.approval_gates:
                    deny("approval_gate_missing", "A consequential operation requires an explicit approval gate.", operation_id)

            gates = set(value.approval_gates)
            if gates - selected_operations:
                deny("approval_gate_without_operation", "Approval gates may name only selected operations.", ",".join(sorted(gates - selected_operations)))
            if required_gates - gates:
                deny("workflow_approval_gate_missing", "The plan omitted an approval gate required by the workflow.", ",".join(sorted(required_gates - gates)))
            acceptance = set(value.acceptance)
            if not acceptance:
                deny("acceptance_missing", "Dispatch requires independently verifiable acceptance criteria.")
            elif required_acceptance - acceptance:
                deny("workflow_acceptance_omitted", "The plan omitted registered workflow acceptance criteria.", ",".join(sorted(required_acceptance - acceptance)))
        elif value.approval_gates or value.acceptance:
            deny("unexpected_execution_contract", "Only dispatch plans may carry approval gates or acceptance criteria.")

        allowed = not denials
        return ResolutionDecision(
            allowed,
            value,
            tuple(denials),
            tuple(sorted(selected_tools)) if allowed else (),
        )

    def _validate(self) -> None:
        for tool in self._tools.values():
            _ensure_disjoint(f"tool {tool.id}", tool.operations)
            for operation in tool.operations:
                if self.capability_registry.get(operation) is None:
                    raise ResolverRegistryError(f"tool {tool.id} references unknown capability {operation}")
                previous = self._operation_tools.get(operation)
                if previous is not None:
                    raise ResolverRegistryError(
                        f"tool operation {operation} is owned by both {previous.id} and {tool.id}"
                    )
                self._operation_tools[operation] = tool

        for skill in self._skills.values():
            _ensure_operation_sets(
                f"skill {skill.id}",
                skill.required_operations,
                skill.optional_operations,
                skill.forbidden_operations,
            )
            for operation in (*skill.required_operations, *skill.optional_operations, *skill.forbidden_operations):
                if operation not in self._operation_tools:
                    raise ResolverRegistryError(f"skill {skill.id} references unknown tool operation {operation}")
            for invocation in skill.invocations:
                if not invocation.startswith("$") or len(invocation) < 2:
                    raise ResolverRegistryError(f"skill {skill.id} has invalid invocation {invocation}")
                key = invocation.casefold()
                previous = self._invocations.get(key)
                if previous is not None:
                    raise ResolverRegistryError(
                        f"skill invocation {invocation} belongs to both {previous.id} and {skill.id}"
                    )
                self._invocations[key] = skill
            if not skill.acceptance:
                raise ResolverRegistryError(f"skill {skill.id} must declare acceptance criteria")

        for workflow in self._workflows.values():
            _ensure_operation_sets(
                f"workflow {workflow.id}",
                workflow.required_operations,
                workflow.optional_operations,
                workflow.forbidden_operations,
            )
            if not workflow.steps:
                raise ResolverRegistryError(f"workflow {workflow.id} must contain at least one skill")
            if len(workflow.steps) != len(set(workflow.steps)):
                raise ResolverRegistryError(f"workflow {workflow.id} repeats a skill step")
            skills: list[SkillManifest] = []
            for skill_id in workflow.steps:
                skill = self.skill(skill_id)
                if skill is None:
                    raise ResolverRegistryError(f"workflow {workflow.id} references unknown skill {skill_id}")
                skills.append(skill)
            known_operations = set(self._operation_tools)
            workflow_operations = set(workflow.allowed_operations) | set(workflow.forbidden_operations)
            unknown = workflow_operations - known_operations
            if unknown:
                raise ResolverRegistryError(f"workflow {workflow.id} references unknown operations {sorted(unknown)}")
            skill_required = {operation for skill in skills for operation in skill.required_operations}
            skill_allowed = {operation for skill in skills for operation in skill.allowed_operations}
            skill_forbidden = {operation for skill in skills for operation in skill.forbidden_operations}
            if not skill_required <= set(workflow.required_operations):
                raise ResolverRegistryError(f"workflow {workflow.id} weakens a required skill operation")
            if not set(workflow.allowed_operations) <= skill_allowed:
                raise ResolverRegistryError(f"workflow {workflow.id} permits an operation outside its skill steps")
            if not skill_forbidden <= set(workflow.forbidden_operations):
                raise ResolverRegistryError(f"workflow {workflow.id} drops a skill prohibition")
            if not set(workflow.approval_gates) <= set(workflow.allowed_operations):
                raise ResolverRegistryError(f"workflow {workflow.id} gates an operation it cannot select")
            if not workflow.acceptance:
                raise ResolverRegistryError(f"workflow {workflow.id} must declare acceptance criteria")


def parse_resolution_plan(value: Mapping[str, Any]) -> ResolutionPlan:
    if not isinstance(value, Mapping):
        raise ResolutionPlanError("ResolutionPlan must be an object")
    _require_exact_fields(value, PLAN_FIELDS, "ResolutionPlan")
    if value.get("schema_version") != "1":
        raise ResolutionPlanError("unsupported ResolutionPlan schema_version")
    admission = _text(value.get("admission"), "admission")
    if admission not in ADMISSIONS:
        raise ResolutionPlanError(f"unsupported admission: {admission}")

    outcome_value = value.get("outcome")
    if not isinstance(outcome_value, Mapping):
        raise ResolutionPlanError("outcome must be an object")
    _require_exact_fields(outcome_value, OUTCOME_FIELDS, "outcome")
    outcome_operation = _text(outcome_value.get("operation"), "outcome operation")
    if outcome_operation not in OUTCOME_OPERATIONS:
        raise ResolutionPlanError(f"unsupported outcome operation: {outcome_operation}")
    outcome = OutcomeResolution(
        outcome_operation,
        _optional_text(outcome_value.get("item_id"), "outcome item_id"),
        _optional_text(outcome_value.get("title"), "outcome title"),
    )

    execution_value = value.get("execution")
    if not isinstance(execution_value, Mapping):
        raise ResolutionPlanError("execution must be an object")
    _require_exact_fields(execution_value, EXECUTION_FIELDS, "execution")
    execution = ExecutionResolution(
        _optional_text(execution_value.get("workflow_id"), "execution workflow_id"),
        _text_tuple(execution_value.get("skill_ids"), "execution skill_ids"),
        _text_tuple(execution_value.get("tool_operations"), "execution tool_operations"),
        _text_tuple(execution_value.get("forbidden_operations"), "execution forbidden_operations"),
    )
    for label, items in (
        ("execution skill_ids", execution.skill_ids),
        ("execution tool_operations", execution.tool_operations),
        ("execution forbidden_operations", execution.forbidden_operations),
    ):
        _ensure_unique_plan_values(items, label)

    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ResolutionPlanError("confidence must be a number from 0 to 1")
    missing_inputs = _text_tuple(value.get("missing_inputs"), "missing_inputs")
    approval_gates = _text_tuple(value.get("approval_gates"), "approval_gates")
    acceptance = _text_tuple(value.get("acceptance"), "acceptance")
    for label, items in (
        ("missing_inputs", missing_inputs),
        ("approval_gates", approval_gates),
        ("acceptance", acceptance),
    ):
        _ensure_unique_plan_values(items, label)
    return ResolutionPlan(
        schema_version="1",
        admission=admission,
        outcome=outcome,
        execution=execution,
        missing_inputs=missing_inputs,
        approval_gates=approval_gates,
        acceptance=acceptance,
        confidence=float(confidence),
        reason=_text(value.get("reason"), "reason"),
    )


def canonicalize_resolution_plan(
    plan: ResolutionPlan,
    registry: ResolverRegistry,
) -> ResolutionPlan:
    """Expand registered Workflow boilerplate without broadening model intent."""
    if plan.admission in {"no_capture", "clarify"}:
        operation = plan.admission
        return replace(
            plan,
            outcome=OutcomeResolution(operation, None, None),
            execution=ExecutionResolution(None, (), (), ()),
            missing_inputs=plan.missing_inputs if operation == "clarify" else (),
            approval_gates=(),
            acceptance=(),
        )
    plan = replace(
        plan,
        execution=replace(
            plan.execution,
            tool_operations=_normalize_operation_ids(
                plan.execution.tool_operations,
                registry,
            ),
            forbidden_operations=_normalize_operation_ids(
                plan.execution.forbidden_operations,
                registry,
            ),
        ),
    )
    workflow_id = plan.execution.workflow_id
    workflow = registry.workflow(workflow_id) if workflow_id else None
    if workflow is None:
        skills = tuple(
            registry.skill(skill_id) for skill_id in plan.execution.skill_ids
        )
        if not skills or any(skill is None for skill in skills):
            return plan
        execution = replace(
            plan.execution,
            tool_operations=_ordered_union(
                *(skill.required_operations for skill in skills if skill is not None),
                plan.execution.tool_operations,
            ),
            forbidden_operations=_ordered_union(
                *(skill.forbidden_operations for skill in skills if skill is not None),
                plan.execution.forbidden_operations,
            ),
        )
        return replace(
            plan,
            execution=execution,
            acceptance=_ordered_union(
                *(skill.acceptance for skill in skills if skill is not None),
                plan.acceptance,
            ),
        )
    proposed_skills = set(plan.execution.skill_ids)
    workflow_skills = set(workflow.steps)
    if not proposed_skills <= workflow_skills:
        return plan
    execution = replace(
        plan.execution,
        skill_ids=workflow.steps,
        tool_operations=_ordered_union(
            workflow.required_operations,
            plan.execution.tool_operations,
        ),
        forbidden_operations=_ordered_union(
            workflow.forbidden_operations,
            plan.execution.forbidden_operations,
        ),
    )
    return replace(
        plan,
        execution=execution,
        approval_gates=_ordered_union(workflow.approval_gates, plan.approval_gates),
        acceptance=_ordered_union(workflow.acceptance, plan.acceptance),
    )


def _load_registry(path: Path, collection: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResolverRegistryError(f"could not load {path}: {error}") from error
    if not isinstance(value, dict) or set(value) != {"registry_version", collection}:
        raise ResolverRegistryError(f"{path.name} must contain only registry_version and {collection}")
    if not isinstance(value[collection], list):
        raise ResolverRegistryError(f"{path.name} {collection} must be an array")
    return value


def _parse_tool(value: Any) -> ToolManifest:
    try:
        row = _manifest(value, TOOL_FIELDS, "tool")
        return ToolManifest(
            _text(row["id"], "tool id"),
            _text(row["name"], "tool name"),
            _text(row["description"], "tool description"),
            _manifest_text_tuple(row["operations"], "tool operations"),
            _version(row["version"], "tool"),
        )
    except ResolutionPlanError as error:
        raise ResolverRegistryError(str(error)) from error


def _parse_skill(value: Any) -> SkillManifest:
    try:
        row = _manifest(value, SKILL_FIELDS, "skill")
        return SkillManifest(
            _text(row["id"], "skill id"),
            _text(row["name"], "skill name"),
            _manifest_text_tuple(row["aliases"], "skill aliases"),
            _manifest_text_tuple(row["invocations"], "skill invocations"),
            _text(row["description"], "skill description"),
            _text(row["when_to_use"], "skill when_to_use"),
            _text(row["when_not_to_use"], "skill when_not_to_use"),
            _manifest_text_tuple(row["inputs"], "skill inputs"),
            _manifest_text_tuple(row["outputs"], "skill outputs"),
            _manifest_text_tuple(row["required_operations"], "skill required_operations"),
            _manifest_text_tuple(row["optional_operations"], "skill optional_operations"),
            _manifest_text_tuple(row["forbidden_operations"], "skill forbidden_operations"),
            _text(row["approval_policy"], "skill approval_policy"),
            _manifest_text_tuple(row["acceptance"], "skill acceptance"),
            _manifest_text_tuple(row["examples"], "skill examples"),
            _manifest_text_tuple(row["counterexamples"], "skill counterexamples"),
            _version(row["version"], "skill"),
        )
    except ResolutionPlanError as error:
        raise ResolverRegistryError(str(error)) from error


def _parse_workflow(value: Any) -> WorkflowManifest:
    try:
        row = _manifest(value, WORKFLOW_FIELDS, "workflow")
        return WorkflowManifest(
            _text(row["id"], "workflow id"),
            _text(row["name"], "workflow name"),
            _manifest_text_tuple(row["aliases"], "workflow aliases"),
            _text(row["description"], "workflow description"),
            _manifest_text_tuple(row["steps"], "workflow steps"),
            _manifest_text_tuple(row["required_operations"], "workflow required_operations"),
            _manifest_text_tuple(row["optional_operations"], "workflow optional_operations"),
            _manifest_text_tuple(row["forbidden_operations"], "workflow forbidden_operations"),
            _manifest_text_tuple(row["approval_gates"], "workflow approval_gates"),
            _manifest_text_tuple(row["acceptance"], "workflow acceptance"),
            _manifest_text_tuple(row["inputs"], "workflow inputs"),
            _manifest_text_tuple(row["outputs"], "workflow outputs"),
            _version(row["version"], "workflow"),
        )
    except ResolutionPlanError as error:
        raise ResolverRegistryError(str(error)) from error


def _manifest(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResolverRegistryError(f"{label} manifest must be an object")
    missing = fields - set(value)
    unknown = set(value) - fields
    if missing or unknown:
        raise ResolverRegistryError(
            f"{label} manifest fields invalid; missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    return value


def _index_unique(values: Iterable[Any], label: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for value in values:
        if value.id in result:
            raise ResolverRegistryError(f"duplicate {label} ID: {value.id}")
        result[value.id] = value
    return result


def _version(value: Any, label: str) -> str:
    version = _text(value, f"{label} version")
    if version != "1":
        raise ResolverRegistryError(f"unsupported {label} version: {version}")
    return version


def _manifest_text_tuple(value: Any, label: str) -> tuple[str, ...]:
    try:
        result = _text_tuple(value, label)
        _ensure_unique_plan_values(result, label)
        return result
    except ResolutionPlanError as error:
        raise ResolverRegistryError(str(error)) from error


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        raise ResolutionPlanError(
            f"{label} fields invalid; missing={sorted(missing)} unknown={sorted(unknown)}"
        )


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolutionPlanError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _text_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ResolutionPlanError(f"{label} must be an array")
    return tuple(_text(item, f"{label} item") for item in value)


def _ensure_unique_plan_values(values: Iterable[str], label: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ResolutionPlanError(f"{label} must not contain duplicates")


def _ensure_disjoint(label: str, values: Iterable[str]) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ResolverRegistryError(f"{label} contains duplicate operations")


def _ensure_operation_sets(
    label: str,
    required: Iterable[str],
    optional: Iterable[str],
    forbidden: Iterable[str],
) -> None:
    required_set = set(required)
    optional_set = set(optional)
    forbidden_set = set(forbidden)
    if required_set & optional_set or required_set & forbidden_set or optional_set & forbidden_set:
        raise ResolverRegistryError(f"{label} operation sets overlap")


def _normalize_phrase(value: str) -> str:
    return re.sub(r"[^a-z0-9$]+", " ", value.casefold()).strip()


def _phrase_present(text: str, phrase: str) -> bool:
    return bool(phrase) and bool(re.search(rf"(?:^| ){re.escape(phrase)}(?: |$)", text))


def _ordered_union(*groups: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for group in groups for value in group))


def _normalize_operation_ids(
    values: Iterable[str],
    registry: ResolverRegistry,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if registry.tool_for_operation(value) is not None:
            normalized.append(value)
            continue
        replacement = next((
            operation
            for tool in registry.tools()
            for operation in tool.operations
            if value == f"{tool.id}.{operation}"
        ), value)
        normalized.append(replacement)
    return tuple(dict.fromkeys(normalized))
