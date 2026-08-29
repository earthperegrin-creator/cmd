"""Deterministic private onboarding for a CMD installation."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cmd_db


BUNDLED_REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registries" / "v1"

DEFAULT_POLICY = {
    "autonomy": "balanced",
    "uncertain_resolution": "ask",
    "external_effects": "exact_payload_approval",
    "destructive_effects": "exact_payload_approval",
    "connectors_default": "disabled",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:72] or "first-outcome"


def normalize_profile(payload: dict[str, Any]) -> dict[str, Any]:
    sources = payload.get("authorized_sources") or []
    outcomes = payload.get("outcomes_90_days") or []
    if isinstance(sources, str):
        sources = [part.strip() for part in sources.split(",") if part.strip()]
    if isinstance(outcomes, str):
        outcomes = [part.strip() for part in outcomes.split(";") if part.strip()]
    first_name = str(payload.get("first_name") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if not first_name:
        raise ValueError("profile.first_name is required")
    if not summary:
        raise ValueError("profile.summary is required")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        raise ValueError("profile.authorized_sources must be a list of paths or labels")
    if not isinstance(outcomes, list) or not all(isinstance(item, str) for item in outcomes):
        raise ValueError("profile.outcomes_90_days must be a list")
    if not outcomes:
        raise ValueError("at least one 90-day outcome is required")
    categories = payload.get("categories") or ["building", "work", "writing", "personal"]
    if not isinstance(categories, list) or not all(isinstance(item, str) for item in categories):
        raise ValueError("profile.categories must be a list")
    return {
        "schema_version": 1,
        "first_name": first_name[:80],
        "summary": summary[:1000],
        "authorized_sources": [item.strip() for item in sources if item.strip()],
        "outcomes_90_days": [item.strip() for item in outcomes if item.strip()],
        "categories": [item.strip() for item in categories if item.strip()],
        "workspace_title": str(payload.get("workspace_title") or f"{first_name} in Command")[:80],
        "workspace_label": str(payload.get("workspace_label") or "LOCAL WORKSPACE")[:80],
        "focus_label": str(payload.get("focus_label") or "HUMAN + AGENTS")[:80],
        "confirmed_at": str(payload.get("confirmed_at") or utc_now()),
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bundled_registry(name: str) -> dict[str, Any]:
    """Load the checked-in universal registry used by both setup and resolution."""
    path = BUNDLED_REGISTRY_DIR / name
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"bundled registry must be an object: {name}")
    return payload


def seed_outcomes(db_path: Path, outcomes: list[str]) -> int:
    cmd_db.init_db(db_path)
    created = 0
    now = utc_now()
    with cmd_db.connect(db_path) as conn:
        for order, title in enumerate(outcomes, 1):
            base = slugify(title)
            item_id = base
            suffix = 2
            while conn.execute("SELECT 1 FROM work_items WHERE item_id=?", (item_id,)).fetchone():
                item_id = f"{base}-{suffix}"
                suffix += 1
            conn.execute(
                """
                INSERT INTO work_items(
                  item_id, title, body, category, urgency, today, status,
                  source_state, created_at, updated_at, last_seen_at,
                  metadata_json, parent_item_id, sort_order
                ) VALUES (?, ?, '', 'work', 'medium', 0, 'open', 'present', ?, ?, ?, ?, NULL, ?)
                """,
                (item_id, title, now, now, now, json.dumps({"created_from": "onboarding"}), order * 10),
            )
            created += 1
    return created


def initialize_private_layer(state_dir: Path, profile_payload: dict[str, Any], *, agent: str = "none") -> dict[str, Any]:
    state = state_dir.expanduser().resolve()
    profile = normalize_profile(profile_payload)
    state.mkdir(parents=True, exist_ok=True)
    for name in ("actions.jsonl", "results.jsonl", "dispatches.jsonl", "approvals.jsonl", "heartbeats.jsonl", "agent-intakes.jsonl"):
        (state / name).touch(exist_ok=True)
    write_json(state / "profile.json", profile)
    write_json(state / "registries" / "tools.json", bundled_registry("tools.json"))
    write_json(state / "registries" / "skills.json", bundled_registry("skills.json"))
    write_json(state / "registries" / "workflows.json", bundled_registry("workflows.json"))
    write_json(state / "policy.json", DEFAULT_POLICY)
    settings_path = state / "settings.json"
    if not settings_path.exists():
        write_json(settings_path, {
            "background_agent": agent,
            "background_model": "",
            "worker_adapter": "public",
            "autonomy_policy": "balanced",
            "gmail_ingestion_enabled": False,
            "email_triage_enabled": False,
            "email_triage_model": "gpt-5.4-mini",
            "workspace_title": profile["workspace_title"],
            "workspace_label": profile["workspace_label"],
            "focus_label": profile["focus_label"],
            "updated_at": utc_now(),
        })
    db_path = state / "cmd.db"
    cmd_db.init_db(db_path)
    existing = cmd_db.list_outcomes(db_path)
    created = 0 if existing else seed_outcomes(db_path, profile["outcomes_90_days"])
    (state / "backups").mkdir(exist_ok=True)
    return {
        "ok": True,
        "state_dir": str(state),
        "profile": str(state / "profile.json"),
        "database": str(db_path),
        "outcomes_created": created,
        "agent": agent,
    }
