#!/usr/bin/env python3
"""Run the same safe fictional consulting canary through public adapters."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cmd_app import onboarding  # noqa: E402


REPORT = ROOT / "evals" / "reports" / "public-worker-canary-latest.json"
PROFILE = {
    "first_name": "Maya",
    "summary": "A fictional independent strategy consultant.",
    "authorized_sources": ["The isolated CMD canary workspace only"],
    "outcomes_90_days": ["Recommend the first market segment for a fictional client"],
}
BRIEFING = """# Fictional client segment briefing

The client can launch only one segment this quarter.

- Segment A: 120 reachable accounts, $18k annual contract, 90-day sales cycle,
  82% pilot retention, high integration effort.
- Segment B: 75 reachable accounts, $32k annual contract, 45-day sales cycle,
  94% pilot retention, medium integration effort.
- Segment C: 210 reachable accounts, $9k annual contract, 30-day sales cycle,
  71% pilot retention, low integration effort.

Choose one segment. Explain the strongest evidence, the main tradeoff, and one
next validation step. All facts are fictional and no external research is
needed or authorized.
"""


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines() if path.exists() else []:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def prepare_state(provider: str) -> Path:
    state = Path(tempfile.mkdtemp(prefix=f"cmd-public-{provider}-canary-"))
    onboarding.initialize_private_layer(state, PROFILE, agent=provider)
    workspace = state / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "briefing.md").write_text(BRIEFING, encoding="utf-8")
    action = {
        "id": "act-public-worker-canary",
        "time": "2026-08-28T00:00:00+00:00",
        "kind": "agent_chat",
        "origin": "public_worker_canary",
        "instruction": "Read briefing.md. Recommend exactly one segment in a concise decision memo with evidence, tradeoff, and next validation step. Do not use the network or perform external actions.",
        "resolved_item_id": "recommend-the-first-market-segment-for-a-fictional-client",
        "binding_authority": "task",
        "status": "queued",
    }
    dispatch = {
        "id": f"dispatch-public-{provider}-canary",
        "time": "2026-08-28T00:00:01+00:00",
        "mode": "canary",
        "status": "ready_for_agent",
        "action_ids": [action["id"]],
        "action_count": 1,
    }
    write_jsonl(state / "actions.jsonl", [action])
    write_jsonl(state / "dispatches.jsonl", [dispatch])
    write_jsonl(state / "results.jsonl", [])
    return state


def evaluate(provider: str, state: Path, completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    receipts = [row for row in read_jsonl(state / "results.jsonl") if row.get("action_id") == "act-public-worker-canary"]
    receipt = receipts[-1] if receipts else {}
    searchable = json.dumps({"conclusion": receipt.get("conclusion"), "artifact": receipt.get("artifact")}, ensure_ascii=False).lower()
    checks = {
        "process_succeeded": completed.returncode == 0,
        "one_receipt": len(receipts) == 1,
        "completed": receipt.get("status") == "completed",
        "recommended_segment_b": "segment b" in searchable,
        "artifact_or_conclusion": bool(receipt.get("artifact") or receipt.get("conclusion")),
        "no_external_operation": not receipt.get("proposed_operation"),
    }
    return {
        "provider": provider,
        "passed": all(checks.values()),
        "checks": checks,
        "receipt": receipt,
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-2000:],
    }


def run_provider(provider: str, *, keep_state: bool = False) -> dict[str, Any]:
    state = prepare_state(provider)
    env = os.environ.copy()
    env["CMD_STATE_DIR"] = str(state)
    command = [
        sys.executable,
        str(ROOT / "scripts" / "public_worker.py"),
        "--provider", provider,
        "--dispatch-id", f"dispatch-public-{provider}-canary",
    ]
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False, timeout=300)
    result = evaluate(provider, state, completed)
    if keep_state:
        result["state_dir"] = str(state)
    else:
        shutil.rmtree(state)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate CMD's generic public worker adapters.")
    parser.add_argument("--provider", choices=["codex", "claude", "both"], default="both")
    parser.add_argument("--keep-state", action="store_true")
    args = parser.parse_args(argv)
    providers = ["codex", "claude"] if args.provider == "both" else [args.provider]
    results = [run_provider(provider, keep_state=args.keep_state) for provider in providers]
    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canary": "fictional_consultant_segment_recommendation",
        "passed": all(result["passed"] for result in results),
        "results": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
