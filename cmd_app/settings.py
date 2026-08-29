"""CMD settings file normalization."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


NowFn = Callable[[], str]
EnsureStateFn = Callable[[], Any]


def default_settings(
    default_email_triage_model: str,
    *,
    profile_defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    profile = profile_defaults or {}
    return {
        "background_agent": "none",
        "background_model": "",
        "worker_adapter": "public",
        "autonomy_policy": "balanced",
        "gmail_ingestion_enabled": False,
        "email_triage_enabled": False,
        "email_triage_model": default_email_triage_model,
        "workspace_title": profile.get("workspace_title") or "My Command",
        "workspace_label": profile.get("workspace_label") or "LOCAL WORKSPACE",
        "focus_label": profile.get("focus_label") or "HUMAN + AGENTS",
        "updated_at": None,
    }


def read_settings(
    settings_file: Path,
    *,
    default_email_triage_model: str,
    autonomy_policies: set[str],
    profile_defaults: dict[str, str] | None = None,
) -> dict[str, Any]:
    settings = default_settings(default_email_triage_model, profile_defaults=profile_defaults)
    if settings_file.exists():
        try:
            payload = json.loads(settings_file.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            settings.update({
                key: payload.get(key)
                for key in settings
                if key in payload
            })
    settings["background_agent"] = settings.get("background_agent") or "none"
    settings["background_model"] = settings.get("background_model") or ""
    settings["worker_adapter"] = settings.get("worker_adapter") or "public"
    settings["gmail_ingestion_enabled"] = settings.get("gmail_ingestion_enabled") is True
    settings["email_triage_enabled"] = settings.get("email_triage_enabled") is True
    settings["email_triage_model"] = settings.get("email_triage_model") or default_email_triage_model
    settings["workspace_title"] = str(settings.get("workspace_title") or "My Command").strip()[:80]
    settings["workspace_label"] = str(settings.get("workspace_label") or "LOCAL WORKSPACE").strip()[:80]
    settings["focus_label"] = str(settings.get("focus_label") or "HUMAN + AGENTS").strip()[:80]
    if settings.get("autonomy_policy") not in autonomy_policies:
        settings["autonomy_policy"] = "balanced"
    return settings


def write_settings(
    settings_file: Path,
    payload: dict[str, Any],
    *,
    default_email_triage_model: str,
    autonomy_policies: set[str],
    profile_defaults: dict[str, str] | None = None,
    ensure_state_dir: EnsureStateFn,
    now_fn: NowFn,
) -> dict[str, Any]:
    ensure_state_dir()
    current = read_settings(
        settings_file,
        default_email_triage_model=default_email_triage_model,
        autonomy_policies=autonomy_policies,
        profile_defaults=profile_defaults,
    )
    agent = str(payload.get("background_agent", current["background_agent"]) or "none").strip().lower()
    model = str(payload.get("background_model", current["background_model"]) or "").strip()
    worker_adapter = str(payload.get("worker_adapter", current["worker_adapter"]) or "public").strip().lower()
    autonomy_policy = str(payload.get("autonomy_policy", current["autonomy_policy"]) or "balanced").strip().lower()
    gmail_ingestion_enabled = bool(payload.get("gmail_ingestion_enabled", current["gmail_ingestion_enabled"]))
    email_triage_enabled = bool(payload.get("email_triage_enabled", current["email_triage_enabled"]))
    email_triage_model = str(payload.get("email_triage_model", current["email_triage_model"]) or default_email_triage_model).strip()
    workspace_title = str(payload.get("workspace_title", current["workspace_title"]) or "My Command").strip()
    workspace_label = str(payload.get("workspace_label", current["workspace_label"]) or "LOCAL WORKSPACE").strip()
    focus_label = str(payload.get("focus_label", current["focus_label"]) or "HUMAN + AGENTS").strip()
    if agent not in {"codex", "claude", "none"}:
        raise ValueError("background_agent must be codex, claude, or none")
    if worker_adapter not in {"public", "private"}:
        raise ValueError("worker_adapter must be public or private")
    if autonomy_policy not in autonomy_policies:
        raise ValueError("autonomy_policy must be balanced, review_drafts, or only_destructive")
    for label, value in {
        "workspace_title": workspace_title,
        "workspace_label": workspace_label,
        "focus_label": focus_label,
    }.items():
        if len(value) > 80:
            raise ValueError(f"{label} must be 80 characters or fewer")
    current.update({
        "background_agent": agent,
        "background_model": model,
        "worker_adapter": worker_adapter,
        "autonomy_policy": autonomy_policy,
        "gmail_ingestion_enabled": gmail_ingestion_enabled,
        "email_triage_enabled": email_triage_enabled,
        "email_triage_model": email_triage_model,
        "workspace_title": workspace_title,
        "workspace_label": workspace_label,
        "focus_label": focus_label,
        "updated_at": now_fn(),
    })
    settings_file.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return current


def background_agent_command(settings: dict[str, Any] | None) -> str:
    settings = settings or {}
    agent = settings.get("background_agent") or "none"
    model = settings.get("background_model") or ""
    worker_adapter = settings.get("worker_adapter") or "private"
    if agent == "none":
        return ""
    if worker_adapter == "public":
        command = f"python3 scripts/public_worker.py --provider {agent} --dispatch-id {{dispatch_id}}"
    elif agent == "codex":
        command = "python3 scripts/codex_worker.py --dispatch-id {dispatch_id}"
    elif agent == "claude":
        command = "python3 scripts/claude_worker.py --dispatch-id {dispatch_id}"
    else:
        return ""
    return f"{command} --model {model}" if model else command
