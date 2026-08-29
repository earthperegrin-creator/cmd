#!/usr/bin/env python3
"""Create or serve a fictional CMD workspace isolated from live user state."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cmd_db  # noqa: E402
from cmd_app import onboarding  # noqa: E402


DEFAULT_STATE_DIR = ROOT / ".cmd-demo"
DEMO_PROFILE = {
    "first_name": "Maya",
    "summary": "A fictional fractional CMO managing startup clients, advisory work, side projects, and family commitments.",
    "authorized_sources": [],
    "outcomes_90_days": [
        "Deliver Alder Health's market-entry recommendation",
        "Prepare Brightfield Climate's investor narrative",
        "Synthesize Juniper Desk's customer research",
        "Grow Maya's consulting practice",
        "Publish Maya's first operator field note",
        "Plan the Chen family Kyoto weekend",
    ],
    "workspace_title": "Maya in Command",
    "workspace_label": "FICTIONAL CONSULTANT WORKSPACE",
    "focus_label": "CLIENTS + AGENTS + LIFE",
}


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def shape_demo_portfolio(database: Path, *, observed_at: str) -> None:
    """Add realistic consulting context and one visible next-move level."""
    outcomes = {
        "deliver-alder-health-s-market-entry-recommendation": (
            "work", "high", 1,
            "Recommend which of three fictional launch regions Alder Health should enter first, with reimbursement, channel, and operating assumptions.",
        ),
        "prepare-brightfield-climate-s-investor-narrative": (
            "work", "high", 1,
            "Turn Brightfield Climate's fictional operating metrics and expansion plan into a credible Series A narrative.",
        ),
        "synthesize-juniper-desk-s-customer-research": (
            "work", "medium", 0,
            "Synthesize twelve fictional customer interviews into product priorities for a B2B support platform.",
        ),
        "grow-maya-s-consulting-practice": (
            "building", "medium", 0,
            "Build a repeatable referral and thought-leadership engine without crowding out client delivery.",
        ),
        "publish-maya-s-first-operator-field-note": (
            "writing", "medium", 0,
            "Turn one useful lesson from client work into a public field note without exposing confidential context.",
        ),
        "plan-the-chen-family-kyoto-weekend": (
            "personal", "low", 0,
            "Confirm the family itinerary around school timing, meals, and one low-stress day with no work calls.",
        ),
    }
    next_moves = [
        ("validate-alder-reimbursement-assumptions", "Validate reimbursement assumptions", "work", "done", 0, "deliver-alder-health-s-market-entry-recommendation"),
        ("compare-alder-launch-regions", "Compare the three launch regions", "work", "open", 1, "deliver-alder-health-s-market-entry-recommendation"),
        ("draft-alder-executive-recommendation", "Draft the executive recommendation", "work", "open", 1, "deliver-alder-health-s-market-entry-recommendation"),
        ("audit-brightfield-metrics", "Audit the metrics behind the story", "work", "open", 1, "prepare-brightfield-climate-s-investor-narrative"),
        ("rewrite-brightfield-deck-story", "Rewrite the ten-slide narrative", "work", "open", 0, "prepare-brightfield-climate-s-investor-narrative"),
        ("code-juniper-interviews", "Code the twelve interview transcripts", "work", "open", 0, "synthesize-juniper-desk-s-customer-research"),
        ("write-juniper-decision-memo", "Write the product decision memo", "work", "open", 0, "synthesize-juniper-desk-s-customer-research"),
        ("follow-up-referral-partners", "Follow up with three referral partners", "building", "open", 0, "grow-maya-s-consulting-practice"),
        ("choose-field-note-thesis", "Choose one useful, non-confidential thesis", "writing", "open", 0, "publish-maya-s-first-operator-field-note"),
        ("confirm-kyoto-lunch-reservation", "Confirm the Saturday lunch reservation", "personal", "open", 0, "plan-the-chen-family-kyoto-weekend"),
    ]
    with cmd_db.connect(database) as conn:
        for item_id, (category, urgency, today, body) in outcomes.items():
            conn.execute(
                "UPDATE work_items SET category=?, urgency=?, today=?, body=?, updated_at=? WHERE item_id=?",
                (category, urgency, today, body, observed_at, item_id),
            )
        for order, (item_id, title, category, status, today, parent) in enumerate(next_moves, 1):
            conn.execute(
                """
                INSERT INTO work_items(
                  item_id, title, body, category, urgency, today, status,
                  source_state, created_at, updated_at, last_seen_at,
                  metadata_json, parent_item_id, sort_order
                ) VALUES (?, ?, '', ?, 'medium', ?, ?, 'present', ?, ?, ?, ?, ?, ?)
                """,
                (item_id, title, category, today, status, observed_at, observed_at, observed_at, json.dumps({"created_from": "sanitized_consultant_demo"}), parent, order * 10),
            )


def create_demo_workspace(state_dir: Path = DEFAULT_STATE_DIR) -> dict[str, object]:
    state = state_dir.expanduser().resolve()
    database = state / "cmd.db"
    if database.exists():
        return {"ok": True, "created": False, "state_dir": str(state), "database": str(database)}
    result = onboarding.initialize_private_layer(state, DEMO_PROFILE, agent="none")
    clock = datetime.now(timezone.utc)
    stamp = lambda minutes_ago: (clock - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    shape_demo_portfolio(database, observed_at=stamp(15))
    actions = [
        {
            "id": "act-demo-alder",
            "time": stamp(12),
            "kind": "agent_chat",
            "instruction": "Compare the three launch regions and recommend one for Alder Health.",
            "resolved_item_id": "deliver-alder-health-s-market-entry-recommendation",
            "binding_authority": "resolver",
            "binding_decision": "attach_outcome",
            "status": "queued",
        },
        {
            "id": "act-demo-brightfield",
            "time": stamp(9),
            "kind": "agent_chat",
            "instruction": "Prepare the exact email sending the revised investor narrative to the fictional client CEO.",
            "resolved_item_id": "prepare-brightfield-climate-s-investor-narrative",
            "binding_authority": "resolver",
            "binding_decision": "attach_outcome",
            "status": "queued",
        },
        {
            "id": "act-demo-juniper",
            "time": stamp(6),
            "kind": "agent_chat",
            "instruction": "Code the twelve customer interviews and synthesize the strongest recurring needs.",
            "resolved_item_id": "synthesize-juniper-desk-s-customer-research",
            "binding_authority": "resolver",
            "binding_decision": "attach_outcome",
            "status": "queued",
        },
        {
            "id": "act-demo-practice",
            "time": stamp(4),
            "kind": "agent_chat",
            "instruction": "Draft a field note using the debrief from last week's workshop.",
            "resolved_item_id": "grow-maya-s-consulting-practice",
            "binding_authority": "resolver",
            "binding_decision": "attach_outcome",
            "status": "queued",
        },
    ]
    results = [
        {
            "action_id": "act-demo-alder",
            "item_id": "deliver-alder-health-s-market-entry-recommendation",
            "time": stamp(10),
            "status": "completed",
            "summary": "Alder Health's market-entry recommendation is ready for review.",
            "conclusion": "Launch in Region West first: it has the clearest fictional reimbursement path and the lowest channel concentration risk, despite a smaller near-term market.",
            "artifact": {"type": "document", "title": "Alder Health market-entry recommendation", "content": "Recommendation: Region West. Why now: reimbursement guidance is already published, two channel partners cover 61% of target clinics, and the pilot can reach contribution break-even with 14 sites. Main risk: slower top-line growth than Region North. Mitigation: preserve Region North as the second-wave option after six months of claims evidence."},
            "needs_human_review": True,
            "human_action": "Review the assumptions and approve the client-ready version.",
            "model": "demo-worker",
        },
        {
            "action_id": "act-demo-brightfield",
            "item_id": "prepare-brightfield-climate-s-investor-narrative",
            "time": stamp(7),
            "status": "awaiting_approval",
            "summary": "The client email is ready for exact approval.",
            "artifact": "To: elena@brightfield.example — Subject: Revised investor narrative — includes the fictional deck link and three material changes.",
            "proposed_operation": {
                "capability": "gmail.send",
                "execution_mode": "execute",
                "risk_level": "external_commit",
                "payload": {"to": ["elena@brightfield.example"], "cc": [], "bcc": [], "subject": "Revised investor narrative", "body": "Hi Elena,\n\nI revised the investor narrative around three points: repeatable project economics, the signed expansion pipeline, and why the next twelve months create a defensible data advantage. The fictional review deck is ready here: https://docs.example/brightfield-narrative\n\nBest,\nMaya"},
            },
            "model": "demo-worker",
        },
        {
            "action_id": "act-demo-practice",
            "item_id": "grow-maya-s-consulting-practice",
            "time": stamp(2),
            "status": "blocked",
            "summary": "The workshop debrief is not available in the authorized demo sources.",
            "conclusion": "Attach the debrief or authorize its location; the worker will not invent workshop evidence.",
            "model": "demo-worker",
        },
    ]
    dispatches = [{"id": "dispatch-demo-consultant", "time": stamp(5), "mode": "launchd", "status": "working", "action_ids": ["act-demo-juniper"], "action_count": 1}]
    heartbeats = [{"dispatch_id": "dispatch-demo-consultant", "time": stamp(1), "state": "working"}]
    write_jsonl(state / "actions.jsonl", actions)
    write_jsonl(state / "results.jsonl", results)
    write_jsonl(state / "dispatches.jsonl", dispatches)
    write_jsonl(state / "heartbeats.jsonl", heartbeats)
    cmd_db.sync_from_state(
        database,
        weekly_log=ROOT / "examples" / "weekly-log.md",
        actions=actions,
        results=results,
        dispatches=dispatches,
        approvals=[],
        report_entries=[],
    )
    return {**result, "created": True}


def reset_demo_workspace(state_dir: Path = DEFAULT_STATE_DIR) -> None:
    target = state_dir.expanduser().resolve()
    if target in {Path("/").resolve(), Path.home().resolve(), ROOT.resolve()} or not target.name.startswith(".cmd-demo"):
        raise ValueError("demo state directory must be named .cmd-demo or .cmd-demo-*")
    if target.exists():
        shutil.rmtree(target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create, reset, or serve CMD's isolated demo.")
    parser.add_argument("command", nargs="?", choices=["create", "reset", "serve"], default="serve")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "reset":
        reset_demo_workspace(args.state_dir)
        print(json.dumps({"ok": True, "reset": str(args.state_dir)}))
        return 0
    result = create_demo_workspace(args.state_dir)
    if args.command == "create":
        print(json.dumps(result, indent=2))
        return 0
    env = os.environ.copy()
    env["CMD_STATE_DIR"] = str(result["state_dir"])
    return subprocess.run([sys.executable, str(ROOT / "server.py"), "--host", args.host, "--port", str(args.port)], cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
