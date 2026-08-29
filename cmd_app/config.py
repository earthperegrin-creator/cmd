"""Portable configuration and private-state locations for CMD."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else default


def user_data_root() -> Path:
    override = os.environ.get("CMD_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "CMD"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    return (Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share") / "cmd"


def state_dir(root: Path, *, preserve_legacy: bool = True) -> Path:
    """Return private runtime state, preserving an existing dogfood install."""
    explicit = os.environ.get("CMD_STATE_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    legacy = root / ".cmd"
    if preserve_legacy and legacy.exists():
        return legacy
    return user_data_root() / "state"


def profile_file(state: Path) -> Path:
    return state / "profile.json"


def read_profile(state: Path) -> dict[str, Any]:
    path = profile_file(state)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def weekly_log_root(root: Path) -> Path:
    return env_path("CMD_WEEKLY_LOG_ROOT", root / "examples")


def startup_db(root: Path, state: Path) -> Path:
    return env_path("CMD_STARTUP_DB", state / "external" / "startup.db")


def sweep_tool(root: Path) -> Path:
    return env_path("CMD_SWEEP_TOOL", root / "scripts" / "sweep.py")


def context_dir() -> Path | None:
    value = os.environ.get("CMD_CONTEXT_DIR", "").strip()
    return Path(value).expanduser() if value else None


def profile_defaults(state: Path) -> dict[str, str]:
    profile = read_profile(state)
    first_name = str(profile.get("first_name") or "").strip()
    title = str(profile.get("workspace_title") or "").strip()
    if not title and first_name:
        title = f"{first_name} in Command"
    return {
        "workspace_title": os.environ.get("CMD_WORKSPACE_TITLE", title or "My Command").strip() or "My Command",
        "workspace_label": os.environ.get("CMD_WORKSPACE_LABEL", str(profile.get("workspace_label") or "LOCAL WORKSPACE")).strip() or "LOCAL WORKSPACE",
        "focus_label": os.environ.get("CMD_FOCUS_LABEL", str(profile.get("focus_label") or "HUMAN + AGENTS")).strip() or "HUMAN + AGENTS",
    }
