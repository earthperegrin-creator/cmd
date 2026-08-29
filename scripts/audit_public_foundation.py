#!/usr/bin/env python3
"""Audit the resolver-independent public-alpha foundation for private coupling."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FOUNDATION = (
    ".env.example", "AGENTS.md", "CLAUDE.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md", "PUBLICATION-BOUNDARY.md", "cmd", "cmd_app/config.py",
    "cmd_app/onboarding.py", "cmd_app/lifecycle.py", "docs/AGENT-SETUP.md",
    "docs/CONFIGURATION.md", "examples/profile.json", "scripts/demo_workspace.py",
    "cmd_app/worker_contract.py", "scripts/public_worker.py", "scripts/run_public_worker_canary.py", "docs/AGENT-INTEGRATION.md", "docs/DEMO-WORKSPACE.md",
    "scripts/run_lifecycle_canary.py", "evals/reports/lifecycle-canary-latest.json",
    "scripts/run_server_security_canary.py", "evals/reports/server-security-canary-latest.json",
    "publication/README.md", "publication/repository-metadata.json", "publication/ship-manifest.txt", "requirements-core.txt",
    "requirements-connectors.txt", "requirements-dev.txt", "examples/weekly-log.md",
    "scripts/build_public_snapshot.py", "scripts/audit_public_snapshot.py",
    "scripts/validate_public_snapshot.py", "test_public_alpha.py",
)
FORBIDDEN = {
    "absolute macOS home path": re.compile(r"/Users/[A-Za-z0-9._-]+"),
    "creator handle": re.compile("earth" + "peregrin", re.IGNORECASE),
    "creator identity": re.compile("And" + r"rew(?:\s+Yang)?", re.IGNORECASE),
    "private organization": re.compile("Dar" + r"win\s+Ventures?", re.IGNORECASE),
    "private workspace": re.compile("ai" + "-sandbox", re.IGNORECASE),
}


def violations() -> list[str]:
    found: list[str] = []
    for relative in FOUNDATION:
        path = ROOT / relative
        if not path.is_file():
            found.append(f"{relative}: missing foundation file")
            continue
        text = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    found.append(f"{relative}:{line_number}: {label}")
    tracked = subprocess.run(
        ["git", "ls-files", ".cmd", ".cmd-demo", "*.db", ".env", ".env.*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    tracked = [path for path in tracked if path != ".env.example"]
    found.extend(f"{path}: tracked runtime or secret file" for path in tracked)
    return found


def main() -> int:
    found = violations()
    if found:
        print("Public-alpha foundation violations:")
        for item in found:
            print(f"- {item}")
        return 1
    print(f"Public-alpha foundation clean across {len(FOUNDATION)} files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
