#!/usr/bin/env python3
"""Resolve and apply one CMD capture through the canonical transaction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cmd_db  # noqa: E402
from cmd_app import capture_observation  # noqa: E402
from cmd_app import task_capture as capture_engine  # noqa: E402


def _structured_request(value: str) -> dict[str, object]:
    if value == "-":
        raw = sys.stdin.read()
    elif value.lstrip().startswith("{"):
        raw = value
    else:
        candidate = Path(value).expanduser()
        try:
            raw = candidate.read_text(encoding="utf-8") if candidate.is_file() else value
        except OSError:
            raw = value
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("structured capture request must be a JSON object")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve and capture work in CMD's SQLite task store.")
    parser.add_argument("title", nargs="?", help="Exact new-outcome title (legacy compatible form)")
    parser.add_argument(
        "--category",
        default="personal",
        choices=("deal", "post", "comms", "auto", "admin", "writing", "networking", "building", "learning", "personal", "trip"),
    )
    parser.add_argument("--body", default=None, help="Optional task detail")
    parser.add_argument("--urgency", choices=("low", "medium", "high"), default=None)
    parser.add_argument("--today", action="store_true", help="Add the task to Today")
    parser.add_argument("--parent", default=None, help="Existing root outcome ID to create this task under")
    parser.add_argument(
        "--request-json",
        default=None,
        help="Canonical structured request as JSON, a JSON file path, or '-' for stdin",
    )
    parser.add_argument("--db", type=Path, default=ROOT / ".cmd" / "cmd.db")
    args = parser.parse_args(argv)

    db_path = args.db.expanduser()
    try:
        if args.request_json is not None:
            request = _structured_request(args.request_json)
            request.setdefault("entrypoint", "structured_cli")
        else:
            if not str(args.title or "").strip():
                parser.error("title is required unless --request-json is supplied")
            title = str(args.title).strip()
            request = capture_engine.build_capture_request(
                title,
                entrypoint="legacy_positional_cli",
                metadata={"createdBy": "agent_cli", "visibleFilter": args.category},
                create_spec={
                    "title": title,
                    "body": args.body if args.body is not None else f"Captured from Command: {title}",
                    "category": args.category,
                    "urgency": args.urgency or "low",
                    "today": args.today,
                    "parent_item_id": args.parent,
                },
            )
        receipt = capture_engine.resolve_and_apply_capture(
            db_path,
            request,
            now_fn=cmd_db.utc_now,
            slugify_fn=cmd_db.slugify_item_id,
        )
        try:
            observation = capture_observation.observe_capture_resolution(
                db_path,
                request,
                receipt,
            )
        except Exception as error:
            observation = {"status": "error", "error": str(error)[-500:]}
        if observation is not None:
            receipt["resolver_shadow"] = observation
    except (ValueError, json.JSONDecodeError) as error:
        print(json.dumps({
            "ok": False,
            "operation": "needs_clarification",
            "error": str(error),
            "database": str(db_path),
        }, ensure_ascii=False))
        return 2
    receipt["database"] = str(db_path)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0 if receipt.get("ok") and receipt.get("verification", {}).get("verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
