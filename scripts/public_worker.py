#!/usr/bin/env python3
"""Run the generic public CMD worker through Codex or Claude Code."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cmd_app import config, worker_contract  # noqa: E402
from scripts.worker_lifecycle import WorkerFiles, run_worker  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a generic CMD background worker.")
    parser.add_argument("--provider", choices=["codex", "claude"], required=True)
    parser.add_argument("--dispatch-id", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    state = config.state_dir(ROOT)
    workspace = state / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    envelope = worker_contract.dispatch_input(state, args.dispatch_id)
    prompt = worker_contract.worker_prompt(envelope, args.model)
    last_message = state / f"last-{args.provider}-worker-message.txt"
    files = WorkerFiles(state / "dispatches.jsonl", state / "results.jsonl", state / "heartbeats.jsonl", last_message)
    if args.dry_run:
        print(prompt)
        return 0

    if args.provider == "codex":
        binary = shutil.which("codex") or "codex"
        command = [
            binary,
            "exec",
            "--cd",
            str(workspace),
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--output-last-message",
            str(last_message),
            prompt,
        ]
        capture_output = False
    else:
        command = [
            shutil.which("claude") or "claude",
            "-p",
            prompt,
            "--restricted",
            "--permission-mode",
            "dontAsk",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
            "--output-format",
            "text",
        ]
        capture_output = True
    code = run_worker(command, dispatch_id=args.dispatch_id, cwd=workspace, files=files, timeout_seconds=int(os.environ.get("CMD_DISPATCH_MAX_RUNTIME_SECONDS", "1800")), capture_output=capture_output)
    if code:
        return code
    try:
        receipts = worker_contract.parse_worker_output(last_message.read_text(encoding="utf-8"), envelope, args.model)
        worker_contract.append_receipts(state / "results.jsonl", receipts)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        from scripts.worker_lifecycle import append_failure_receipts
        append_failure_receipts(files, args.dispatch_id, f"Background agent returned an invalid typed result: {error}")
        print(f"invalid worker result: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
