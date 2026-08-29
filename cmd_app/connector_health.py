"""Connector health cache and capability mapping for CMD."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ParseIsoDatetimeFn = Callable[[str | None], datetime | None]
NowFn = Callable[[], datetime]


def default_connector_health() -> dict[str, Any]:
    capabilities = {
        capability: {
            "ok": None,
            "service": service,
            "check": "not checked yet",
            "executor": executor,
            "live": live,
            "detail": "Run connector check to verify this path.",
        }
        for capability, service, executor, live in [
            ("gmail.read", "gmail", "CMD Google service", True),
            ("gmail.draft", "gmail", "CMD Google service", True),
            ("gmail.send", "gmail", "CMD Google service", False),
            ("calendar.read", "calendar", "CMD Google service", True),
            ("calendar.create", "calendar", "CMD Google service", True),
            ("calendar.update", "calendar", "CMD Google service", True),
            ("drive.read", "drive", "CMD Google service", True),
            ("drive.copy", "drive", "CMD Google service", True),
            ("drive.download", "drive", "CMD Google service", True),
            ("drive.export", "drive", "CMD Google service", True),
            ("drive.create", "drive", "CMD Google service", True),
            ("buffer.channels", "buffer", "CMD Buffer service", True),
            ("buffer.posts", "buffer", "CMD Buffer service", True),
            ("buffer.draft", "buffer", "CMD Buffer service", True),
            ("buffer.schedule", "buffer", "CMD Buffer service", False),
            ("buffer.publish", "buffer", "CMD Buffer service", False),
        ]
    }
    return {
        "ok": None,
        "generated_at": None,
        "include_live": None,
        "checks": [],
        "services": [],
        "capabilities": capabilities,
        "source": "default",
    }


def read_connector_health(health_file: Path) -> dict[str, Any]:
    fallback = default_connector_health()
    if not health_file.exists():
        return fallback
    try:
        payload = json.loads(health_file.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {**fallback, "source": "invalid_cache"}
    if not isinstance(payload, dict):
        return fallback
    return {
        **fallback,
        **payload,
        "capabilities": {
            **fallback["capabilities"],
            **(payload.get("capabilities") if isinstance(payload.get("capabilities"), dict) else {}),
        },
        "source": "cache",
    }


def read_gmail_monitor_status(
    status_file: Path,
    *,
    parse_iso_datetime_fn: ParseIsoDatetimeFn,
    now_fn: NowFn,
) -> dict[str, Any]:
    fallback = {
        "ok": False,
        "state": "unavailable",
        "last_check_at": "",
        "last_success_at": "",
        "consecutive_failures": 0,
        "detail": "Gmail monitor has not reported yet.",
    }
    if not status_file.exists():
        return fallback
    try:
        payload = json.loads(status_file.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {**fallback, "state": "error", "detail": "Gmail monitor status is unreadable."}
    if not isinstance(payload, dict):
        return fallback
    status = {**fallback, **payload}
    checked_at = parse_iso_datetime_fn(str(status.get("last_check_at") or ""))
    if checked_at:
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        interval = max(30, int(status.get("interval_seconds") or 120))
        if (now_fn() - checked_at).total_seconds() > max(600, interval * 3):
            status.update({
                "ok": False,
                "state": "stale",
                "detail": "Gmail monitor has stopped reporting.",
            })
    return status


def action_required_capabilities(action: dict[str, Any]) -> list[str]:
    metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    operation = metadata.get("proposed_operation") if isinstance(metadata.get("proposed_operation"), dict) else {}
    capability = str(operation.get("capability") or "")
    if not capability:
        return []
    requirements = {
        "gmail.draft": ["gmail.read", "gmail.draft"],
        "gmail.send": ["gmail.read", "gmail.send"],
        "calendar.create": ["calendar.read", "calendar.create"],
        "calendar.update": ["calendar.read", "calendar.update"],
        "drive.create": ["drive.read", "drive.create"],
        "drive.upload": ["drive.read", "drive.create"],
        "google_drive.upload": ["drive.read", "drive.create"],
        "drive.copy": ["drive.read", "drive.copy"],
        "drive.download": ["drive.read", "drive.download"],
        "drive.export": ["drive.read", "drive.export"],
        "callmemo.execute": ["drive.read", "drive.create"],
        "callmemo_email.execute": ["drive.read", "drive.download", "drive.export", "gmail.draft"],
        "buffer.schedule": ["buffer.channels", "buffer.schedule"],
        "buffer.publish": ["buffer.channels", "buffer.publish"],
    }
    return requirements.get(capability, [capability])


def connector_status_for_action(action: dict[str, Any], health: dict[str, Any]) -> dict[str, Any]:
    capabilities = health.get("capabilities") if isinstance(health.get("capabilities"), dict) else {}
    required = action_required_capabilities(action)
    missing = []
    unknown = []
    for capability in required:
        status = capabilities.get(capability) if isinstance(capabilities.get(capability), dict) else {}
        ok = status.get("ok")
        if ok is False:
            missing.append({"capability": capability, **status})
        elif ok is None:
            unknown.append({"capability": capability, **status})
    return {
        "required": required,
        "missing": missing,
        "unknown": unknown,
        "ok": not missing,
        "checked_at": health.get("generated_at"),
    }
