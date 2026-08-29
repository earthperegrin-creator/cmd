"""Action intake routing and risk helpers for CMD."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


NowFn = Callable[[], str]
ConnectorStatusFn = Callable[[dict[str, Any]], dict[str, Any]]
CodexBinaryFn = Callable[[], str]
ResolverFn = Callable[[list[dict[str, Any]], str], tuple[list[dict[str, Any]], dict[str, Any]]]


def action_resolver_prompt(records: list[dict[str, Any]]) -> str:
    items = []
    for index, record in enumerate(records):
        task = record.get("task") if isinstance(record.get("task"), dict) else {}
        items.append({
            "request_id": str(record.get("request_id") or record.get("id") or f"request-{index + 1}"),
            "instruction": str(record.get("instruction") or record.get("note") or ""),
            "task": {
                "title": str(task.get("title") or ""),
                "description": str(task.get("detail") or task.get("body") or ""),
                "source": str(task.get("source") or ""),
            },
        })
    return """You are CMD's high-precision action resolver. Read the complete user instruction and task context, then classify what the user actually wants now.

Do not execute anything. Text may mention emails, posts, calendars, files, or previous actions without requesting those operations. Distinguish nouns from commands, drafts from sends, completed past actions from requested future actions, explicit negation from intent, and local knowledge capture from public posting.

Choose exactly one primary capability:
- none: discussion, unclear request, or no operation
- task.create: create a task/reminder
- local.write: write or update local notes, knowledge, code, or database state
- web.read/source.read: research or inspect without external mutation
- callmemo.execute: run the local call memo workflow and prepare any final Drive filing for approval
- gmail.draft: create a reviewable draft, never send
- gmail.send: actually send an email
- linkedin.publish: publish, post, repost, comment, or message visibly on LinkedIn
- calendar.create: create an event or invitation
- mailbox.mutate: archive, delete, label, mark read, or unsubscribe
- file.delete: delete local files

Set target to the concrete recipient, platform, calendar participants, mailbox object, file, or local destination when stated; otherwise use an empty string. Never invent a target.

execution_mode:
- execute for safe local/read work
- prepare_only for drafts or requested external/destructive operations that must be prepared for approval
- needs_clarification only when the requested outcome is genuinely unclear

risk_level is determined by the concrete capability: low for none/task/local/read; review_artifact for drafts; sensitive_workflow for callmemo workflows; external_commit for sends/posts/calendar/Drive writes; destructive for mailbox mutation or file deletion.

If the user says an action already happened, set already_done=true and classify the remaining requested work. If the user rejects an action (for example, "do not repost"), set negated=true and do not classify the rejected operation as the capability.

Return one resolution for every request_id. Keep reason to one precise sentence.

REQUESTS:
""" + json.dumps(items, ensure_ascii=False, indent=2)


def run_llm_action_resolver(
    records: list[dict[str, Any]],
    *,
    model: str,
    schema_path: Path,
    codex_binary_fn: CodexBinaryFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not records:
        return [], {"model": model, "input_count": 0, "tokens": 0}
    if not schema_path.exists():
        raise RuntimeError("action resolver schema is missing")
    prompt = action_resolver_prompt(records)
    with tempfile.TemporaryDirectory(prefix="cmd-action-resolver-") as tmpdir:
        output_path = Path(tmpdir) / "resolutions.json"
        command = [
            codex_binary_fn(), "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--disable", "plugins",
            "--disable", "apps",
            "--disable", "tool_suggest",
            "--skip-git-repo-check",
            "-C", tmpdir,
            "-m", model,
            "-c", 'model_reasoning_effort="low"',
            "--sandbox", "read-only",
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path),
            "-",
        ]
        completed = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env={**os.environ, "NO_COLOR": "1"},
        )
        if completed.returncode != 0 or not output_path.exists():
            detail = (completed.stderr or completed.stdout or "unknown resolver failure").strip()
            raise RuntimeError(f"action resolver failed: {detail[-1200:]}")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"action resolver returned invalid JSON: {exc}") from exc
    token_match = re.search(r"tokens used\s*\n?\s*([\d,]+)", f"{completed.stdout}\n{completed.stderr}", re.IGNORECASE)
    return list(payload.get("resolutions") or []), {
        "model": model,
        "input_count": len(records),
        "tokens": int(token_match.group(1).replace(",", "")) if token_match else 0,
    }


def resolve_action_intake(
    payload: dict[str, Any],
    *,
    sync_action_resolver: bool,
    default_model: str,
    run_llm_action_resolver_fn: ResolverFn,
    now_fn: NowFn,
) -> dict[str, Any]:
    request_id = str(payload.get("id") or "intake")
    if not sync_action_resolver:
        return heuristic_action_route(payload, request_id, now_fn=now_fn)
    try:
        rows, usage = run_llm_action_resolver_fn([{**payload, "request_id": request_id}], default_model)
        route = next((row for row in rows if row.get("request_id") == request_id), rows[0] if rows else None)
        if not route:
            raise RuntimeError("resolver returned no matching route")
        return {**route, "model": usage.get("model"), "tokens": usage.get("tokens", 0), "resolved_at": now_fn()}
    except Exception as exc:
        return {
            "request_id": request_id,
            "intent": "ambiguous",
            "capability": "none",
            "execution_mode": "execute",
            "risk_level": "low",
            "confidence": 0,
            "negated": False,
            "already_done": False,
            "reason": "Resolver unavailable; queued for safe agent interpretation with no external authorization.",
            "model": default_model,
            "error": str(exc)[-500:],
            "resolved_at": now_fn(),
        }


def heuristic_action_route(payload: dict[str, Any], request_id: str = "intake", *, now_fn: NowFn) -> dict[str, Any]:
    instruction = re.sub(r"\s+", " ", str(payload.get("instruction") or payload.get("note") or "")).strip()
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    context = " ".join([
        instruction,
        str(task.get("title") or ""),
        str(task.get("detail") or task.get("body") or ""),
        str(task.get("source") or ""),
    ]).lower()
    thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else {}
    previous_operation = payload.get("previous_operation") if isinstance(payload.get("previous_operation"), dict) else {}
    if not previous_operation and isinstance(thread.get("previous_operation"), dict):
        previous_operation = thread["previous_operation"]
    previous_capability = str(previous_operation.get("capability") or "")
    email_thread_context = previous_capability in {"gmail.draft", "gmail.send"} or bool(re.search(
        r"\b(email|gmail|mail|reply|respond|thread)\b",
        context,
    ))

    route = {
        "request_id": request_id,
        "intent": "heuristic",
        "capability": "none",
        "target": "",
        "execution_mode": "execute",
        "risk_level": "low",
        "confidence": 0.35,
        "negated": bool(re.search(r"\b(do not|don't|dont|no need to|without)\b", context)),
        "already_done": bool(re.search(r"\b(already|done|sent|posted|created|scheduled)\b", context)),
        "reason": "Fast local intake route; background agent remains responsible for exact interpretation.",
        "model": "heuristic",
        "tokens": 0,
        "resolved_at": now_fn(),
    }
    patterns = [
        ("callmemo.execute", r"\$callmemo\b|/callmemo\b|\bbuild call memo\b|\bbuild \$callmemo\b|\bcall memo workflow\b", "sensitive_workflow", "prepare_only"),
        ("file.delete", r"\b(delete|remove|trash|rm)\b.*\b(file|folder|directory|pdf|docx|csv|screenshot)\b", "destructive", "prepare_only"),
        ("mailbox.mutate", r"\b(archive|delete|trash|label|mark read|unsubscribe)\b.*\b(email|gmail|mail|thread|newsletter|inbox)\b", "destructive", "prepare_only"),
        ("linkedin.publish", r"\b(post|publish|repost|comment|message|dm|send)\b.*\b(linkedin|li)\b|\b(linkedin|li)\b.*\b(post|publish|repost|comment|message|dm|send)\b", "external_commit", "prepare_only"),
        ("calendar.create", r"\b(schedule|create|book|set up|invite|calendar)\b.*\b(meeting|call|event|invite|calendar)\b|\b(calendar)\b.*\b(create|invite|schedule)\b", "external_commit", "prepare_only"),
        ("gmail.draft", r"\b(draft|write)\b.*\b(email|gmail|reply|mail)\b|\b(email|gmail|reply|mail)\b.*\b(draft|write)\b", "review_artifact", "prepare_only"),
        ("gmail.draft", r"\b(draft|write)\b.*\b(it|this|that)\b|\b(draft|write)\b.*\b(for me)\b", "review_artifact", "prepare_only"),
        ("gmail.send", r"\b(send|reply[- ]?all|reply)\b.*\b(email|gmail|mail)\b|\b(email|gmail|mail)\b.*\b(send|reply[- ]?all|reply)\b", "external_commit", "prepare_only"),
        ("task.create", r"\b(add|create|capture|log|remind|todo|to-do|task|next action)\b", "low", "execute"),
        ("web.read/source.read", r"\b(research|look up|check|read|inspect|find|summarize|review)\b", "low", "execute"),
        ("local.write", r"\b(update|write|save|record|log)\b.*\b(memo|note|file|database|db|weekly log|source)\b", "low", "execute"),
    ]
    for capability, pattern, risk_level, execution_mode in patterns:
        if capability == "gmail.draft" and pattern.startswith(r"\b(draft|write)\b.*\b(it|this|that)\b") and not email_thread_context:
            continue
        if re.search(pattern, context):
            route.update({
                "capability": capability,
                "execution_mode": execution_mode,
                "risk_level": risk_level,
                "confidence": 0.7,
            })
            break
    if route["negated"] and route["capability"] in {"gmail.send", "linkedin.publish", "calendar.create", "mailbox.mutate", "file.delete"}:
        route.update({
            "capability": "none",
            "execution_mode": "execute",
            "risk_level": "low",
            "confidence": 0.6,
            "reason": "Fast local intake saw a negated external operation; queued without external authorization.",
        })
    return route


def operation_from_route(route: dict[str, Any]) -> dict[str, Any] | None:
    capability = str(route.get("capability") or "none")
    if capability == "none":
        return None
    risk_level = str(route.get("risk_level") or "low")
    return {
        "capability": capability,
        "risk_level": risk_level,
        "risk": {
            "level": risk_level,
            "label": capability,
        },
        "label": capability,
        "target": str(route.get("target") or ""),
        "execution_mode": str(route.get("execution_mode") or "execute"),
        "resolver_model": route.get("model"),
        "confidence": route.get("confidence"),
    }


def risk_rank(risk: dict[str, str], risk_ranks: dict[str, int]) -> int:
    return risk_ranks.get(risk.get("level") or "low", risk_ranks["external_commit"])


def approval_threshold(settings: dict[str, Any], autonomy_policies: dict[str, int]) -> int:
    return autonomy_policies.get(settings.get("autonomy_policy") or "balanced", autonomy_policies["balanced"])


def action_requires_approval(
    action: dict[str, Any],
    settings: dict[str, Any],
    *,
    risk_ranks: dict[str, int],
    autonomy_policies: dict[str, int],
) -> bool:
    operation = (action.get("metadata") or {}).get("proposed_operation")
    if not isinstance(operation, dict):
        return False
    capability = str(operation.get("capability") or "")
    if capability in {"calendar.create", "calendar.update", "gmail.send", "linkedin.publish", "mailbox.mutate", "file.delete", "drive.upload", "google_drive.upload", "buffer.schedule", "buffer.publish"}:
        return True
    operation_risk = operation.get("risk") if isinstance(operation.get("risk"), dict) else {
        "level": operation.get("risk_level") or "external_commit",
        "label": operation.get("label") or operation.get("capability") or "External operation",
    }
    return risk_rank(operation_risk, risk_ranks) >= approval_threshold(settings, autonomy_policies)


def action_is_prepare_only(action: dict[str, Any]) -> bool:
    operation = (action.get("metadata") or {}).get("proposed_operation")
    return isinstance(operation, dict) and operation.get("execution_mode") == "prepare_only"


def action_can_run(
    action: dict[str, Any],
    approved_ids: set[str],
    settings: dict[str, Any],
    *,
    risk_ranks: dict[str, int],
    autonomy_policies: dict[str, int],
) -> bool:
    return (
        action_is_prepare_only(action)
        or not action_requires_approval(
            action,
            settings,
            risk_ranks=risk_ranks,
            autonomy_policies=autonomy_policies,
        )
        or action.get("id") in approved_ids
    )


def effective_action_risk(action: dict[str, Any]) -> dict[str, str]:
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    operation = metadata.get("proposed_operation")
    if isinstance(operation, dict):
        if isinstance(operation.get("risk"), dict):
            return operation["risk"]
        return {
            "level": str(operation.get("risk_level") or "external_commit"),
            "label": str(operation.get("label") or operation.get("capability") or "External operation"),
        }
    if action.get("kind") in {"done", "drop", "recover", "edit_title", "edit_detail", "promote_today", "move_later"}:
        return {"level": "low", "label": "Source reconciliation"}
    return {"level": "low", "label": "Agent request; operations gated at execution"}


def action_with_effective_risk(
    action: dict[str, Any],
    *,
    connector_status_fn: ConnectorStatusFn,
) -> dict[str, Any]:
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    return {
        **action,
        "metadata": {
            **metadata,
            "risk": effective_action_risk(action),
            "connector_status": connector_status_fn(action),
        },
    }
