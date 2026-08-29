"""Generic, provider-neutral worker input and typed-result contract."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_STATUSES = {"completed", "awaiting_approval", "blocked", "failed"}
JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)


def read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def dispatch_input(state_dir: Path, dispatch_id: str) -> dict[str, Any]:
    dispatch = next((row for row in read_jsonl(state_dir / "dispatches.jsonl") if row.get("id") == dispatch_id), None)
    if not dispatch:
        raise ValueError(f"dispatch not found: {dispatch_id}")
    action_ids = {str(value) for value in dispatch.get("action_ids") or []}
    actions = [row for row in read_jsonl(state_dir / "actions.jsonl") if str(row.get("id")) in action_ids]
    existing = {str(row.get("action_id")) for row in read_jsonl(state_dir / "results.jsonl") if row.get("status") in ALLOWED_STATUSES}
    actions = [row for row in actions if str(row.get("id")) not in existing]
    return {
        "schema_version": 1,
        "dispatch": {"id": dispatch_id, "mode": dispatch.get("mode"), "created_at": dispatch.get("time")},
        "actions": actions,
        "profile": read_json(state_dir / "profile.json", {}),
        "policy": read_json(state_dir / "policy.json", {}),
        "tools": read_json(state_dir / "registries" / "tools.json", {"tools": []}),
        "skills": read_json(state_dir / "registries" / "skills.json", {"skills": []}),
        "workflows": read_json(state_dir / "registries" / "workflows.json", {"workflows": []}),
    }


def worker_prompt(envelope: dict[str, Any], model: str = "") -> str:
    return f"""You are a background worker for CMD, a local human-agent command center.

Perform only the actions in the attached dispatch envelope. Use only the context, Tools, Skills, and authority represented there. Do not discover unrelated personal files or grant yourself a missing operation.

This public-alpha envelope is not a compiled JobSpec. Registry entries describe
the universal vocabulary; they do not prove that a connector is live or that an
operation is granted. If the envelope lacks the concrete input or executable
authority needed for a claim, return `blocked`.

Safety contract:
- Research, analysis, summaries, and reviewable drafts are safe when their sources are authorized.
- If information or authority is missing, return `blocked` with the precise dependency.
- Third-party-visible, destructive, financial, calendar, publishing, purchase, and send operations require approval of the exact payload. Preparation is not approval.
- Never claim completion without a reviewable artifact, source evidence, or a verifiable external receipt.
- Keep one action lineage. Do not create a second CMD outcome for an intermediate step.

Return only one JSON object with this shape:
{{"schema_version":1,"dispatch_id":"...","receipts":[{{"action_id":"...","status":"completed|awaiting_approval|blocked|failed","summary":"one sentence","conclusion":"decision-relevant result","artifact":{{}},"needs_human_review":false,"human_action":"optional","proposed_operation":{{"capability":"...","execution_mode":"execute","risk_level":"external_commit","payload":{{}}}},"sources":[],"model":"{model}"}}]}}

`proposed_operation` is required for `awaiting_approval` and must contain every material field. Omit optional fields when they add no value.

Dispatch envelope:
{json.dumps(envelope, ensure_ascii=False, indent=2)}
"""


def parse_worker_output(raw: str, envelope: dict[str, Any], model: str = "") -> list[dict[str, Any]]:
    text = raw.strip()
    if not text.startswith("{"):
        fenced = JSON_FENCE.findall(text)
        if len(fenced) != 1:
            raise ValueError("worker output must contain exactly one JSON object")
        text = fenced[0]
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("worker output must be a schema_version 1 object")
    if payload.get("dispatch_id") != envelope["dispatch"]["id"]:
        raise ValueError("worker output dispatch_id does not match")
    allowed_ids = {str(action.get("id")) for action in envelope.get("actions") or []}
    receipts = payload.get("receipts")
    if not isinstance(receipts, list):
        raise ValueError("worker output receipts must be a list")
    seen: set[str] = set()
    normalized = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise ValueError("each receipt must be an object")
        action_id = str(receipt.get("action_id") or "")
        status = str(receipt.get("status") or "")
        summary = str(receipt.get("summary") or "").strip()
        if action_id not in allowed_ids or action_id in seen:
            raise ValueError("worker returned an unknown or duplicate action_id")
        if status not in ALLOWED_STATUSES or not summary:
            raise ValueError("worker receipt requires an allowed status and summary")
        if status == "completed":
            artifact = receipt.get("artifact")
            has_artifact = (
                isinstance(artifact, dict) and any(value not in (None, "", [], {}) for value in artifact.values())
            ) or (isinstance(artifact, str) and bool(artifact.strip()))
            has_conclusion = bool(str(receipt.get("conclusion") or "").strip())
            has_sources = isinstance(receipt.get("sources"), list) and bool(receipt["sources"])
            if not (has_artifact or has_conclusion or has_sources):
                raise ValueError("completed receipt requires an artifact, conclusion, or source evidence")
        if status == "awaiting_approval":
            operation = receipt.get("proposed_operation")
            if not isinstance(operation, dict) or not isinstance(operation.get("payload"), dict):
                raise ValueError("awaiting_approval requires an exact proposed_operation payload")
        row = dict(receipt)
        row.update({"action_id": action_id, "status": status, "summary": summary, "time": now, "model": receipt.get("model") or model or "background-worker"})
        normalized.append(row)
        seen.add(action_id)
    if seen != allowed_ids:
        raise ValueError("worker must return exactly one receipt for every pending dispatch action")
    return normalized


def append_receipts(path: Path, receipts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for receipt in receipts:
            handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
