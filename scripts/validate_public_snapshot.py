#!/usr/bin/env python3
"""Validate a public snapshot in a disposable Git repository."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Validation imports the auditor. Keep a pristine checkout pristine before the
# whole-snapshot audit runs.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_public_snapshot import audit  # noqa: E402


def run(command: list[str], *, cwd: Path, timeout: int = 180) -> dict[str, object]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    return {
        "command": " ".join(command),
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "output": (completed.stdout + completed.stderr)[-2000:],
    }


def validate(snapshot: Path, *, with_providers: bool = False) -> dict[str, object]:
    source = snapshot.expanduser().resolve()
    problems = audit(source)
    checks: list[dict[str, object]] = [{
        "name": "whole_snapshot_audit",
        "passed": not problems,
        "detail": "all exported files match the manifest and content policy" if not problems else "; ".join(problems[:10]),
    }]
    if problems:
        return {"ok": False, "snapshot": str(source), "checks": checks}

    with tempfile.TemporaryDirectory(prefix="cmd-public-validation-") as tmpdir:
        staged = Path(tmpdir) / "snapshot"
        shutil.copytree(source, staged)
        git_commands = (
            ["git", "init", "-b", "main"],
            ["git", "config", "user.name", "CMD Snapshot Validator"],
            ["git", "config", "user.email", "validator@example.invalid"],
            ["git", "add", "."],
            ["git", "commit", "-m", "Public snapshot validation"],
        )
        for command in git_commands:
            result = run(command, cwd=staged, timeout=30)
            if not result["passed"]:
                checks.append({"name": "disposable_git", **result})
                return {"ok": False, "snapshot": str(source), "checks": checks}
        checks.append({"name": "disposable_git", "passed": True, "detail": "initialized an isolated release checkout"})

        commands = [
            ("public_tests", [sys.executable, "-m", "unittest", "test_public_alpha.py"], 120),
            ("lifecycle_canary", [sys.executable, "scripts/run_lifecycle_canary.py"], 180),
            ("server_security_canary", [sys.executable, "scripts/run_server_security_canary.py"], 120),
        ]
        if with_providers:
            commands.insert(
                1,
                ("worker_provider_canary", [sys.executable, "scripts/run_public_worker_canary.py", "--provider", "both"], 300),
            )
        for name, command, timeout in commands:
            result = run(command, cwd=staged, timeout=timeout)
            checks.append({"name": name, **result})
            if not result["passed"]:
                break
    return {"ok": all(bool(check.get("passed")) for check in checks), "snapshot": str(source), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a built CMD public snapshot.")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument(
        "--with-providers",
        action="store_true",
        help="also launch locally installed Codex and Claude CLIs; requires provider authentication",
    )
    args = parser.parse_args(argv)
    try:
        receipt = validate(args.snapshot, with_providers=args.with_providers)
    except (OSError, subprocess.TimeoutExpired) as error:
        receipt = {"ok": False, "snapshot": str(args.snapshot), "error": str(error)}
    print(json.dumps(receipt, indent=2))
    return 0 if receipt.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
